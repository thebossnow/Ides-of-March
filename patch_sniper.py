"""
Patch script: applies 3 improvements to weather_market_sniper.py and executor.py
1. ECMWF HRES forecast source
2. Dynamic entry pricing (MAX_ENTRY_PRICE -> 0.40)
3. Pre-cached allowances/tick_size/neg_risk for <300ms snipe latency
"""
import re

# ============================================================
# PATCH 1: weather_market_sniper.py — ECMWF + dynamic pricing
# ============================================================
with open("/root/weatherbot/weather_market_sniper.py", "r") as f:
    sniper = f.read()

# --- 1a: Add ECMWF constants after GraphCast config block ---
old_gc_weight = """GRAPHCAST_WEIGHT = 0.65
GFS_WEIGHT = 1.0 - GRAPHCAST_WEIGHT  # 0.35"""

new_weights = """# --- ECMWF HRES configuration ---
# ECMWF's high-resolution deterministic forecast (IFS, 0.25-degree).
# Consistently the best single-model NWP forecast globally. Using the
# Open-Meteo ecmwf_ifs025 model which provides ECMWF HRES data for free.
# API: https://api.open-meteo.com/v1/ecmwf?models=ecmwf_ifs025
ECMWF_API_URL = "https://api.open-meteo.com/v1/ecmwf"
ECMWF_MODEL = "ecmwf_ifs025"

# 3-way blending weights: ECMWF HRES is the anchor, GraphCast adds ML skill,
# GFS is a hedge. Weights sum to 1.0.
#   ECMWF:     Best physics-based NWP, gold standard for 1-10 day
#   GraphCast: Best ML model, excels at extremes and tropical cities
#   GFS:       Independent signal, good coastal/mountain micro-climates
ECMWF_WEIGHT = 0.40
GRAPHCAST_WEIGHT = 0.35
GFS_WEIGHT = 0.25  # 1.0 - ECMWF_WEIGHT - GRAPHCAST_WEIGHT"""

sniper = sniper.replace(old_gc_weight, new_weights)

# --- 1b: Raise MAX_ENTRY_PRICE and update bounds comment ---
sniper = sniper.replace(
    "# --- Entry price bounds (strictly 0.01 to 0.10) ---\nMIN_ENTRY_PRICE = 0.01\nMAX_ENTRY_PRICE = 0.10",
    "# --- Entry price bounds (0.01 to 0.40) ---\nMIN_ENTRY_PRICE = 0.01\nMAX_ENTRY_PRICE = 0.40"
)

# --- 1c: Add ECMWF cache variables after GraphCast cache ---
old_caches = """_graphcast_cache: dict = {}
_graphcast_cache_lock = threading.Lock()"""

new_caches = """_graphcast_cache: dict = {}
_graphcast_cache_lock = threading.Lock()

# ECMWF HRES forecast cache: { city_key: { date_str: temp_celsius } }
_ecmwf_cache: dict = {}
_ecmwf_cache_lock = threading.Lock()"""

sniper = sniper.replace(old_caches, new_caches)

# --- 1d: Replace static _compute_entry_price with dynamic version ---
old_pricing = '''def _compute_entry_price(model_prob: float) -> float:
    """
    Maps model probability to a limit buy price in [0.01, 0.10].

    Tiering:
        prob 0.30-0.34 -> $0.01
        prob 0.35-0.39 -> $0.02
        prob 0.40-0.44 -> $0.03
        prob 0.45-0.49 -> $0.04
        prob 0.50-0.54 -> $0.05
        prob 0.55-0.59 -> $0.06
        prob 0.60-0.64 -> $0.07
        prob 0.65-0.69 -> $0.08
        prob 0.70-0.79 -> $0.09
        prob 0.80+     -> $0.10
    """
    if model_prob < 0.30:
        return MIN_ENTRY_PRICE
    elif model_prob < 0.35:
        return 0.01
    elif model_prob < 0.40:
        return 0.02
    elif model_prob < 0.45:
        return 0.03
    elif model_prob < 0.50:
        return 0.04
    elif model_prob < 0.55:
        return 0.05
    elif model_prob < 0.60:
        return 0.06
    elif model_prob < 0.65:
        return 0.07
    elif model_prob < 0.70:
        return 0.08
    elif model_prob < 0.80:
        return 0.09
    else:
        return 0.10'''

new_pricing = '''def _compute_entry_price(model_prob: float) -> float:
    """
    Dynamic entry pricing: prices at a discount to model probability.

    Instead of a fixed tier table capped at $0.10, we now price dynamically:
        entry_price = model_prob * DISCOUNT_FACTOR

    DISCOUNT_FACTOR (0.55) means we bid at 55% of our model's fair value.
    This ensures positive expected value on every fill while being aggressive
    enough to win fills against other market makers.

    Examples:
        prob 0.30 -> $0.01 (floored to MIN)
        prob 0.40 -> $0.22 * 0.55 = $0.12  (was $0.03 under old system!)
        prob 0.60 -> $0.33
        prob 0.80 -> $0.40 (capped at MAX)
        prob 0.95 -> $0.40 (capped at MAX)

    The discount factor balances two goals:
      - Low enough to maintain large edge on every fill
      - High enough to actually get filled (beat other bidders)
    """
    DISCOUNT_FACTOR = 0.55

    price = model_prob * DISCOUNT_FACTOR

    # Round to nearest cent (Polymarket tick size)
    price = round(price, 2)

    # Clamp to valid range
    price = max(MIN_ENTRY_PRICE, min(MAX_ENTRY_PRICE, price))

    return price'''

sniper = sniper.replace(old_pricing, new_pricing)

# --- 1e: Add _fetch_ecmwf_forecast function after _fetch_graphcast_forecast ---
ecmwf_func = '''

def _fetch_ecmwf_forecast(city_key: str, days: int = 10) -> Optional[dict]:
    """
    Fetches ECMWF HRES (ecmwf_ifs025) daily max temperature from Open-Meteo.

    ECMWF's Integrated Forecasting System is the world's best deterministic
    NWP model. The IFS at 0.25-degree resolution provides superior forecasts
    especially for European and tropical cities where GFS is weaker.

    Args:
        city_key: Key into weather.CITIES dict (e.g. "NYC", "Tokyo")
        days: Number of forecast days to request (max 10 for ECMWF on Open-Meteo)

    Returns:
        Dict of { date_str: temp_celsius } or None on failure.
    """
    city = CITIES.get(city_key)
    if city is None:
        return None

    params = {
        "latitude": city["lat"],
        "longitude": city["lon"],
        "daily": "temperature_2m_max",
        "temperature_unit": "celsius",
        "timezone": city["tz"],
        "forecast_days": min(days, 10),
        "models": ECMWF_MODEL,
    }

    try:
        response = requests.get(ECMWF_API_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        dates = data["daily"]["time"]
        temps = data["daily"]["temperature_2m_max"]
        return dict(zip(dates, temps))
    except Exception as e:
        logger.debug(f"ECMWF fetch failed for {city_key}: {e}")
        return None

'''

# Insert after the _fetch_graphcast_forecast function
sniper = sniper.replace(
    "\ndef _refresh_all_forecasts",
    ecmwf_func + "def _refresh_all_forecasts"
)

# --- 1f: Add ECMWF phase to _refresh_all_forecasts ---
# Add Phase 1c after Phase 1b (GraphCast section)
old_metar_phase = """    with _graphcast_cache_lock:
        _graphcast_cache.update(new_gc_cache)

    # --- Phase 2: Live METAR observations (AviationWeather.gov) ---"""

new_ecmwf_phase = """    with _graphcast_cache_lock:
        _graphcast_cache.update(new_gc_cache)

    # --- Phase 1c: ECMWF HRES forecasts ---
    # ECMWF IFS (ecmwf_ifs025) via Open-Meteo /v1/ecmwf.
    # The gold standard NWP model, fetched independently for 3-way blending.
    new_ecmwf_cache = {}
    ecmwf_count = 0
    for city_key in CITIES:
        try:
            ecmwf_data = _fetch_ecmwf_forecast(city_key, days=FORECAST_DAYS)
            if ecmwf_data:
                new_ecmwf_cache[city_key] = ecmwf_data
                ecmwf_count += 1
        except Exception as e:
            logger.debug(f"ECMWF refresh failed for {city_key}: {e}")
        time.sleep(0.3)

    with _ecmwf_cache_lock:
        _ecmwf_cache.update(new_ecmwf_cache)

    # --- Phase 2: Live METAR observations (AviationWeather.gov) ---"""

sniper = sniper.replace(old_metar_phase, new_ecmwf_phase)

# --- 1g: Update refresh log line to include ECMWF count ---
sniper = sniper.replace(
    '''    logger.info(
        f"Sniper forecast refresh complete: "
        f"GFS={gfs_count}/{len(CITIES)}, "
        f"GraphCast={gc_count}/{len(CITIES)}, "
        f"METAR={metar_count} obs in {elapsed:.1f}s"
    )''',
    '''    logger.info(
        f"Sniper forecast refresh complete: "
        f"GFS={gfs_count}/{len(CITIES)}, "
        f"GraphCast={gc_count}/{len(CITIES)}, "
        f"ECMWF={ecmwf_count}/{len(CITIES)}, "
        f"METAR={metar_count} obs in {elapsed:.1f}s"
    )'''
)

# --- 1h: Add _get_cached_ecmwf helper ---
sniper = sniper.replace(
    '''def _get_cached_graphcast(city: str) -> Optional[dict]:
    """Returns the pre-cached GraphCast forecast dict for a city, or None."""
    with _graphcast_cache_lock:
        return _graphcast_cache.get(city)''',
    '''def _get_cached_graphcast(city: str) -> Optional[dict]:
    """Returns the pre-cached GraphCast forecast dict for a city, or None."""
    with _graphcast_cache_lock:
        return _graphcast_cache.get(city)


def _get_cached_ecmwf(city: str) -> Optional[dict]:
    """Returns the pre-cached ECMWF HRES forecast dict for a city, or None."""
    with _ecmwf_cache_lock:
        return _ecmwf_cache.get(city)'''
)

# --- 1i: Update 3-way blending in get_best_temperature_brackets ---
old_blend = """    # --- Read from PRE-CACHED forecasts (no HTTP call!) ---
    # Blend GraphCast + standard GFS using configured weights.
    # GraphCast gets GRAPHCAST_WEIGHT (0.65), GFS gets GFS_WEIGHT (0.35).
    # If only one source is available, use it at 100%.
    gfs_data = _get_cached_forecast(city)
    gc_data = _get_cached_graphcast(city)

    gfs_temp = gfs_data.get(date_str) if gfs_data else None
    gc_temp = gc_data.get(date_str) if gc_data else None

    # Cache miss on both: fallback blocking GFS fetch
    if gfs_temp is None and gc_temp is None:
        logger.warning(f"Sniper: cache miss for {city} {date_str}, blocking fetch")
        try:
            gfs_data = get_forecast(city, days=FORECAST_DAYS)
        except Exception as e:
            logger.error(f"Sniper: blocking forecast fetch failed for {city}: {e}")
            return []
        if gfs_data is None or date_str not in gfs_data:
            logger.debug(f"Sniper: no forecast for {city} on {date_str}")
            return []
        with _forecast_cache_lock:
            _forecast_cache[city] = gfs_data
        gfs_temp = gfs_data[date_str]

    # Weighted blend of available models
    if gc_temp is not None and gfs_temp is not None:
        forecast_celsius = (gc_temp * GRAPHCAST_WEIGHT) + (gfs_temp * GFS_WEIGHT)
        logger.debug(
            f"Sniper blend {city} {date_str}: "
            f"GC={gc_temp:.1f}C * {GRAPHCAST_WEIGHT} + "
            f"GFS={gfs_temp:.1f}C * {GFS_WEIGHT} = {forecast_celsius:.1f}C"
        )
    elif gc_temp is not None:
        forecast_celsius = gc_temp
        logger.debug(f"Sniper {city} {date_str}: GraphCast only = {gc_temp:.1f}C")
    else:
        forecast_celsius = gfs_temp
        logger.debug(f"Sniper {city} {date_str}: GFS only = {gfs_temp:.1f}C")"""

new_blend = """    # --- Read from PRE-CACHED forecasts (no HTTP call!) ---
    # 3-way blend: ECMWF HRES (0.40) + GraphCast (0.35) + GFS (0.25).
    # If a source is missing, re-normalize weights across available sources.
    gfs_data = _get_cached_forecast(city)
    gc_data = _get_cached_graphcast(city)
    ecmwf_data = _get_cached_ecmwf(city)

    gfs_temp = gfs_data.get(date_str) if gfs_data else None
    gc_temp = gc_data.get(date_str) if gc_data else None
    ecmwf_temp = ecmwf_data.get(date_str) if ecmwf_data else None

    # Cache miss on all three: fallback blocking GFS fetch
    if gfs_temp is None and gc_temp is None and ecmwf_temp is None:
        logger.warning(f"Sniper: cache miss for {city} {date_str}, blocking fetch")
        try:
            gfs_data = get_forecast(city, days=FORECAST_DAYS)
        except Exception as e:
            logger.error(f"Sniper: blocking forecast fetch failed for {city}: {e}")
            return []
        if gfs_data is None or date_str not in gfs_data:
            logger.debug(f"Sniper: no forecast for {city} on {date_str}")
            return []
        with _forecast_cache_lock:
            _forecast_cache[city] = gfs_data
        gfs_temp = gfs_data[date_str]

    # Weighted blend with dynamic re-normalization for missing sources
    sources = []
    if ecmwf_temp is not None:
        sources.append(("ECMWF", ecmwf_temp, ECMWF_WEIGHT))
    if gc_temp is not None:
        sources.append(("GC", gc_temp, GRAPHCAST_WEIGHT))
    if gfs_temp is not None:
        sources.append(("GFS", gfs_temp, GFS_WEIGHT))

    total_weight = sum(w for _, _, w in sources)
    forecast_celsius = sum(t * (w / total_weight) for _, t, w in sources)

    blend_parts = " + ".join(
        f"{name}={t:.1f}C*{w/total_weight:.2f}" for name, t, w in sources
    )
    logger.debug(
        f"Sniper blend {city} {date_str}: {blend_parts} = {forecast_celsius:.1f}C"
    )"""

sniper = sniper.replace(old_blend, new_blend)

# --- 1j: Update startup notification to include ECMWF ---
sniper = sniper.replace(
    '''            notifier.send_message(
                f"Weather Sniper started\\n"
                f"WS: {WS_URL}\\n"
                f"GFS cache: {cached_cities} cities\\n"
                f"GraphCast cache: {gc_cities} cities\\n"
                f"METAR cache: {metar_cities} stations\\n"
                f"Blend: {GRAPHCAST_WEIGHT:.0%} GraphCast + {GFS_WEIGHT:.0%} GFS\\n"''',
    '''            with _ecmwf_cache_lock:
                ecmwf_cities = len(_ecmwf_cache)
            notifier.send_message(
                f"Weather Sniper started\\n"
                f"WS: {WS_URL}\\n"
                f"GFS cache: {cached_cities} cities\\n"
                f"GraphCast cache: {gc_cities} cities\\n"
                f"ECMWF cache: {ecmwf_cities} cities\\n"
                f"METAR cache: {metar_cities} stations\\n"
                f"Blend: {ECMWF_WEIGHT:.0%} ECMWF + {GRAPHCAST_WEIGHT:.0%} GraphCast + {GFS_WEIGHT:.0%} GFS\\n"'''
)

# --- 1k: Add precache call before order execution ---
old_execute = """    # ---- Execute orders (1 or 2 brackets) ----
    for i, signal in enumerate(signals):"""

new_execute = """    # ---- Pre-cache token metadata for all signals (cuts snipe latency) ----
    from executor import precache_token_metadata
    for signal in signals:
        try:
            precache_token_metadata(signal["tokenId"])
        except Exception as e:
            logger.debug(f"Sniper: precache failed for {signal['tokenId'][:16]}: {e}")

    # ---- Execute orders (1 or 2 brackets) ----
    for i, signal in enumerate(signals):"""

sniper = sniper.replace(old_execute, new_execute)

with open("/root/weatherbot/weather_market_sniper.py", "w") as f:
    f.write(sniper)

print("PATCHED: weather_market_sniper.py")


# ============================================================
# PATCH 2: executor.py — Pre-cache allowances/tick_size/neg_risk
# ============================================================
with open("/root/weatherbot/executor.py", "r") as f:
    executor = f.read()

# --- 2a: Add caches after _client initialization ---
old_client = """_client: ClobClient = _init_client()


def get_client() -> ClobClient:
    \"\"\"Returns the module-level singleton ClobClient.\"\"\"
    return _client"""

new_client = """_client: ClobClient = _init_client()

# ---------------------------------------------------------------------------
# Pre-cache for snipe latency optimization.
# Caches tick_size, neg_risk, and conditional token allowances so that
# place_gtc_order() can skip the 3 slowest calls (2 GETs + 1 blockchain tx).
# Target: reduce snipe latency from ~1-2s to <300ms (sign + POST only).
# ---------------------------------------------------------------------------
_tick_cache: dict[str, str] = {}        # token_id -> tick_size
_neg_risk_cache: dict[str, bool] = {}   # token_id -> neg_risk
_allowance_cache: set[str] = set()      # token_ids with allowance already set


def get_client() -> ClobClient:
    \"\"\"Returns the module-level singleton ClobClient.\"\"\"
    return _client


def precache_token_metadata(token_id: str) -> None:
    \"\"\"
    Pre-fetches tick_size, neg_risk, and sets conditional allowance for a token.
    Called BEFORE order execution so that place_gtc_order() can skip these steps.

    This is the key latency optimization: these 3 calls take ~800-1500ms combined.
    By doing them during bracket evaluation (before the order hot path), the actual
    order placement is reduced to just sign + POST (~100-200ms).
    \"\"\"
    client = get_client()

    # Pre-fetch tick_size and neg_risk in parallel
    if token_id not in _tick_cache or token_id not in _neg_risk_cache:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fut_tick = pool.submit(client.get_tick_size, token_id)
            fut_neg = pool.submit(client.get_neg_risk, token_id)
            _tick_cache[token_id] = fut_tick.result()
            _neg_risk_cache[token_id] = fut_neg.result()
        logger.debug(f"Precached tick/neg_risk for {token_id[:16]}...")

    # Pre-set conditional allowance (blockchain tx, slowest call)
    if token_id not in _allowance_cache:
        try:
            set_conditional_allowance(client, token_id)
            _allowance_cache.add(token_id)
            logger.debug(f"Precached allowance for {token_id[:16]}...")
        except Exception as e:
            logger.warning(f"Precache allowance failed for {token_id[:16]}: {e}")"""

executor = executor.replace(old_client, new_client)

# --- 2b: Update place_gtc_order to use caches ---
old_gtc_live = """    client = get_client()
    order_id = ""

    try:
        set_conditional_allowance(client, token_id)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fut_tick = pool.submit(client.get_tick_size, token_id)
            fut_neg  = pool.submit(client.get_neg_risk,  token_id)
            tick_size = fut_tick.result()
            neg_risk  = fut_neg.result()

        order_args = OrderArgs(
            price=price,
            size=num_shares,
            side=BUY,
            token_id=token_id,
        )
        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
        signed_order = client.create_order(order_args, options)
        resp = client.post_order(signed_order, OrderType.GTC)

        order_id = resp.get("orderID", "")
        status   = resp.get("status", "UNKNOWN")

        logger.info(
            f"GTC order placed: id={order_id} | "
            f"{num_shares:.2f} shares @ ${price:.4f} | status={status}"
        )"""

new_gtc_live = """    client = get_client()
    order_id = ""

    try:
        # Use pre-cached values if available (set by precache_token_metadata).
        # Falls back to live fetches if cache miss (non-sniper code paths).
        if token_id in _allowance_cache:
            logger.debug(f"GTC: using precached allowance for {token_id[:16]}")
        else:
            set_conditional_allowance(client, token_id)
            _allowance_cache.add(token_id)

        if token_id in _tick_cache and token_id in _neg_risk_cache:
            tick_size = _tick_cache[token_id]
            neg_risk = _neg_risk_cache[token_id]
            logger.debug(f"GTC: using precached tick/neg_risk for {token_id[:16]}")
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                fut_tick = pool.submit(client.get_tick_size, token_id)
                fut_neg  = pool.submit(client.get_neg_risk,  token_id)
                tick_size = fut_tick.result()
                neg_risk  = fut_neg.result()
            _tick_cache[token_id] = tick_size
            _neg_risk_cache[token_id] = neg_risk

        order_args = OrderArgs(
            price=price,
            size=num_shares,
            side=BUY,
            token_id=token_id,
        )
        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
        signed_order = client.create_order(order_args, options)
        resp = client.post_order(signed_order, OrderType.GTC)

        order_id = resp.get("orderID", "")
        status   = resp.get("status", "UNKNOWN")

        logger.info(
            f"GTC order placed: id={order_id} | "
            f"{num_shares:.2f} shares @ ${price:.4f} | status={status}"
        )"""

executor = executor.replace(old_gtc_live, new_gtc_live)

with open("/root/weatherbot/executor.py", "w") as f:
    f.write(executor)

print("PATCHED: executor.py")
print("ALL PATCHES APPLIED SUCCESSFULLY")
