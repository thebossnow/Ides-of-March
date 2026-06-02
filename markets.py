"""
markets.py - Polymarket global CLOB/Gamma API market discovery and parsing.
Uses Gamma API (no auth) for market metadata and
py-clob-client for authenticated price fetching.
"""

import os
import re
import logging
from datetime import datetime
from dotenv import load_dotenv
from py_clob_client.client import ClobClient

load_dotenv()
logger = logging.getLogger(__name__)

# API endpoints
GAMMA_URL  = "https://gamma-api.polymarket.com/markets"
CLOB_HOST  = "https://clob.polymarket.com"
CHAIN_ID   = 137  # Polygon Mainnet

# Signature type: 1 = EOA/Magic.link proxy, 2 = Gnosis Safe-style proxy
# Set POLYMARKET_SIG_TYPE=1 in .env to override (default: 2)
SIG_TYPE   = int(os.getenv("POLYMARKET_SIG_TYPE", "2"))

# Keywords to identify weather/temperature markets
# Based on confirmed Polymarket question format:
# "Will the high temperature in [City] be [X]°F or higher on [date]?"
WEATHER_KEYWORDS = [
    "°f", "°c", "degrees f", "degrees c",
    "temperature", "high temperature",
    "central park", "high temp",
]

# City aliases for matching market questions
# Expanded to match Polymarket's confirmed phrasing patterns.
# Keys are checked against lowercased question text. Values must match
# weather.py CITIES keys exactly. More specific aliases must come first
# in iteration order (dict preserves insertion order in Python 3.7+).
CITY_ALIASES = {
    # North America
    "new york city":    "NYC",
    "new york's":       "NYC",
    "new york":         "NYC",
    "nyc":              "NYC",
    "central park":     "NYC",
    "chicago":          "Chicago",
    "los angeles":      "LA",
    "miami":            "Miami",
    "denver":           "Denver",
    "washington dc":    "DC",
    "washington":       "DC",
    "san francisco":    "San Francisco",
    "houston":          "Houston",
    "austin":           "Austin",
    "dallas":           "Dallas",
    "atlanta":          "Atlanta",
    "seattle":          "Seattle",
    "toronto":          "Toronto",
    "mexico city":      "Mexico City",
    # South America
    "sao paulo":        "Sao Paulo",
    "são paulo":        "Sao Paulo",
    "buenos aires":     "Buenos Aires",
    # Europe
    "london":           "London",
    "paris":            "Paris",
    "madrid":           "Madrid",
    "milan":            "Milan",
    "munich":           "Munich",
    "warsaw":           "Warsaw",
    "moscow":           "Moscow",
    "istanbul":         "Istanbul",
    "ankara":           "Ankara",
    "tel aviv":         "Tel Aviv",
    # Asia
    "tokyo":            "Tokyo",
    "seoul":            "Seoul",
    "beijing":          "Beijing",
    "shanghai":         "Shanghai",
    "chengdu":          "Chengdu",
    "chongqing":        "Chongqing",
    "wuhan":            "Wuhan",
    "shenzhen":         "Shenzhen",
    "hong kong":        "Hong Kong",
    "taipei":           "Taipei",
    "singapore":        "Singapore",
    "lucknow":          "Lucknow",
    # Oceania
    "wellington":       "Wellington",
}


def get_client() -> ClobClient:
    """Creates and returns an authenticated CLOB client."""
    pk     = os.getenv("POLYMARKET_PRIVATE_KEY")
    funder = os.getenv("POLYMARKET_FUNDER")
    if not pk or not funder:
        raise EnvironmentError(
            "POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER must be set in .env"
        )
    client = ClobClient(
        CLOB_HOST,
        key=pk,
        chain_id=CHAIN_ID,
        signature_type=SIG_TYPE,  # Configurable: set POLYMARKET_SIG_TYPE in .env (default: 2)
        funder=funder.lower().strip(),  # Normalize: lowercase + strip whitespace
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def get_weather_markets() -> list:
    """
    Fetches active, unresolved weather markets from the Gamma API.

    Strategy (two-pass):
    1. Slug search: query for 'highest-temperature' directly — fast and precise.
    2. Full scan fallback: if slug search returns nothing, page through all
       active markets and keyword-filter. This handles any Gamma API changes.

    The `closed` filter is intentionally omitted: Polymarket sometimes marks
    same-day markets as closed=true before they fully resolve, which would
    cause us to miss valid trading opportunities.
    """
    import requests

    # --- Pass 1: targeted slug search ---
    slug_markets = []
    for slug_prefix in ("highest-temperature", "high-temperature"):
        try:
            resp = requests.get(
                GAMMA_URL,
                params={"slug_url": slug_prefix, "active": "true", "limit": 100},
                timeout=15,
            )
            resp.raise_for_status()
            batch = resp.json()
            if isinstance(batch, list):
                slug_markets.extend(batch)
        except Exception as e:
            logger.debug(f"Slug search '{slug_prefix}' failed: {e}")

    # Deduplicate by conditionId
    seen = set()
    deduped = []
    for m in slug_markets:
        cid = m.get("conditionId", m.get("slug", ""))
        if cid and cid not in seen:
            seen.add(cid)
            deduped.append(m)

    if deduped:
        logger.info(f"Found {len(deduped)} weather markets via slug search")
        return deduped

    # --- Pass 2: full scan fallback ---
    logger.info("Slug search returned 0 results — falling back to full market scan")
    all_markets = []
    offset = 0
    page_size = 100
    total_scanned = 0

    while True:
        params = {
            "active": "true",
            "limit":  page_size,
            "offset": offset,
        }
        try:
            response = requests.get(GAMMA_URL, params=params, timeout=15)
            response.raise_for_status()
            batch = response.json()
        except Exception as e:
            logger.error(f"Failed to fetch markets (offset={offset}): {e}")
            break

        if not batch:
            break

        total_scanned += len(batch)
        for m in batch:
            question = m.get("question", "").lower()
            slug = m.get("slug", "").lower()
            if any(kw in question or kw in slug for kw in WEATHER_KEYWORDS):
                all_markets.append(m)

        if len(batch) < page_size:
            break
        offset += page_size

    logger.info(f"Found {len(all_markets)} weather markets (full scan: {total_scanned} total)")
    return all_markets


def _parse_json_field(value) -> list:
    """
    Safely parses a field that may be a JSON string or already a list.
    Polymarket Gamma API returns clobTokenIds and outcomes as JSON strings.
    Example: '["123", "456"]' -> ["123", "456"]
    """
    import json
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def get_yes_token_id(market: dict) -> str | None:
    """
    Returns the tokenId for the YES outcome of a binary market.
    clobTokenIds[0] = YES token, clobTokenIds[1] = NO token (Polymarket convention).
    Confirmed from live market data: outcomes=["Yes","No"], clobTokenIds=[yes_id, no_id]
    """
    token_ids = _parse_json_field(market.get("clobTokenIds", []))
    outcomes  = _parse_json_field(market.get("outcomes", []))

    # Match by outcome label first
    for i, outcome in enumerate(outcomes):
        if outcome.upper() in ("YES", "TRUE") and i < len(token_ids):
            return str(token_ids[i])

    # Fall back to first token (YES by convention)
    if token_ids:
        return str(token_ids[0])
    return None


def parse_market_metadata(market: dict) -> dict | None:
    """
    Attempts to extract city, date, and temperature bucket from a market.
    Tries the human-readable question first, then falls back to parsing
    the slug (which has a deterministic format on Polymarket).
    Returns a metadata dict or None if unparseable.
    """
    question     = market.get("question", "")
    slug         = market.get("slug", "")
    condition_id = market.get("conditionId", "")

    # --- Try question-based parsing first ---
    city_key = _identify_city(question)
    date_str = _extract_date(question) if city_key else None
    bounds   = _extract_temp_bounds(question) if date_str else None

    # --- Fall back to slug-based parsing if any field failed ---
    if not city_key or not date_str or not bounds:
        slug_result = _parse_from_slug(slug)
        if slug_result:
            city_key = city_key or slug_result.get("city")
            date_str = date_str or slug_result.get("date")
            bounds   = bounds   or slug_result.get("bounds")

    if not city_key:
        logger.debug(f"Could not identify city in: {question} / {slug}")
        return None
    if not date_str:
        logger.debug(f"Could not extract date from: {question} / {slug}")
        return None
    if not bounds:
        logger.debug(f"Could not extract temp bounds from: {question} / {slug}")
        return None

    yes_token_id = get_yes_token_id(market)

    return {
        "city":          city_key,
        "date":          date_str,
        "bucket_low":    bounds["low"],
        "bucket_high":   bounds["high"],
        "unit":          bounds["unit"],
        "question":      question,
        "condition_id":  condition_id,
        "yes_token_id":  yes_token_id,
    }


def _identify_city(text: str) -> str | None:
    """Matches city aliases against lowercased text."""
    t = text.lower()
    for alias, key in CITY_ALIASES.items():
        if alias in t:
            return key
    return None


# Reverse lookup: slug fragment -> CITIES key.
# Built from CITY_ALIASES so there's one source of truth.
# "new-york" -> "NYC", "los-angeles" -> "LA", "mexico-city" -> "Mexico City", etc.
_SLUG_CITY_MAP: dict = {}


def _build_slug_city_map() -> None:
    """Populate _SLUG_CITY_MAP from CITY_ALIASES (called once on first use)."""
    if _SLUG_CITY_MAP:
        return
    seen_values: set = set()
    for alias, city_key in CITY_ALIASES.items():
        slug_form = alias.replace(" ", "-")
        if slug_form not in _SLUG_CITY_MAP:
            _SLUG_CITY_MAP[slug_form] = city_key
        seen_values.add(city_key)


def _parse_from_slug(slug: str) -> dict | None:
    """
    Parses city, date, and temperature bucket from a Polymarket weather slug.

    Slug format:
        highest-temperature-in-{city}-on-{month}-{day}-{year}-{temp_suffix}

    Temp suffix examples:
        20corbelow   -> low=None, high=20, unit=C
        30corhigher  -> low=30,   high=None, unit=C
        10c          -> low=10,   high=11,  unit=C   (exact degree bucket)
        84forhigher  -> low=84,   high=None, unit=F
        75forbelow   -> low=None, high=75,  unit=F
        52-53f       -> low=52,   high=53,  unit=F   (range)

    Returns dict with keys: city, date, bounds  (or None if unparseable)
    """
    _build_slug_city_map()

    s = slug.lower().strip()

    # Must be a weather market slug
    if not s.startswith("highest-temperature-in-"):
        return None

    # Strip prefix
    rest = s[len("highest-temperature-in-"):]  # e.g. "chengdu-on-april-3-2026-20corbelow"

    # Find "-on-" to split city from date+temp
    on_idx = rest.find("-on-")
    if on_idx < 0:
        return None

    city_slug = rest[:on_idx]           # "chengdu" or "los-angeles" or "mexico-city"
    after_on  = rest[on_idx + 4:]       # "april-3-2026-20corbelow"

    # City lookup
    city_key = _SLUG_CITY_MAP.get(city_slug)
    if not city_key:
        # Try partial match for hyphenated cities
        for slug_frag, ckey in _SLUG_CITY_MAP.items():
            if city_slug == slug_frag:
                city_key = ckey
                break
        if not city_key:
            return None

    # Parse date: "{month}-{day}-{year}-..." e.g. "april-3-2026-20corbelow"
    month_map = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    date_match = re.match(
        r'(january|february|march|april|may|june|july|august|september|'
        r'october|november|december)-(\d{1,2})-(\d{4})-(.+)',
        after_on
    )
    if not date_match:
        return None

    month_num = month_map[date_match.group(1)]
    day       = int(date_match.group(2))
    year      = int(date_match.group(3))
    temp_part = date_match.group(4)  # e.g. "20corbelow", "52-53f", "10c"

    try:
        date_str = datetime(year, month_num, day).strftime("%Y-%m-%d")
    except ValueError:
        return None

    # Parse temp suffix
    bounds = _parse_slug_temp(temp_part)
    if not bounds:
        return None

    return {"city": city_key, "date": date_str, "bounds": bounds}


def _parse_slug_temp(temp_part: str) -> dict | None:
    """
    Parses the temperature suffix from a slug.
    Examples: "20corbelow", "84forhigher", "52-53f", "10c"
    """
    t = temp_part.strip()

    # Range: "52-53f" or "52-53c"
    m = re.match(r'^(\d+)-(\d+)([fc])$', t)
    if m:
        unit = "F" if m.group(3) == "f" else "C"
        return {"low": float(m.group(1)), "high": float(m.group(2)), "unit": unit}

    # "Xforbelow" or "Xcorbelow" (or below)
    m = re.match(r'^(\d+)([fc])orbelow$', t)
    if m:
        unit = "F" if m.group(2) == "f" else "C"
        return {"low": None, "high": float(m.group(1)), "unit": unit}

    # "Xforhigher" or "Xcorhigher" (or higher)
    m = re.match(r'^(\d+)([fc])orhigher$', t)
    if m:
        unit = "F" if m.group(2) == "f" else "C"
        return {"low": float(m.group(1)), "high": None, "unit": unit}

    # Exact degree: "10c" or "65f" (bucket = [X, X+1))
    m = re.match(r'^(\d+)([fc])$', t)
    if m:
        val  = float(m.group(1))
        unit = "F" if m.group(2) == "f" else "C"
        return {"low": val, "high": val + 1.0, "unit": unit}

    return None


def get_market_price(token_id: str, client: ClobClient = None) -> float | None:
    """
    Fetches the last trade price for a given token ID via the CLOB API.
    Returns float (0.01-0.99) or None on failure.

    Pass a pre-created client (from get_client()) to reuse credentials
    across multiple calls in the same cycle. If None, a new client is
    created internally (backward-compatible, but slower).
    """
    try:
        if client is None:
            client = get_client()
        result = client.get_last_trade_price(token_id)
        if result and "price" in result:
            return float(result["price"])
        return None
    except Exception as e:
        logger.warning(f"Could not fetch price for token {token_id}: {e}")
        return None


# Spread threshold: books wider than this are considered "illiquid" and
# get routed to the ladder-bid path instead of using the midpoint.
ILLIQUID_SPREAD = 0.50


def _parse_book_sides(book) -> tuple:
    """
    Extracts (bids, asks) from a get_order_book() response.
    Polymarket's py_clob_client changed return type from an OrderBook object
    (with .bids/.asks attributes) to a plain dict ({'bids': [...], 'asks': [...]}).
    This helper handles both formats transparently.
    """
    if isinstance(book, dict):
        return book.get("bids") or [], book.get("asks") or []
    return getattr(book, "bids", None) or [], getattr(book, "asks", None) or []


def _entry_price(entry) -> float:
    """Extracts price from an order book entry (dict {'price': ...} or object with .price)."""
    if isinstance(entry, dict):
        return float(entry["price"])
    return float(entry.price)


def get_midpoint_price(token_id: str, client: ClobClient = None) -> float | None:
    """
    Fetches the midpoint (bid+ask)/2 for a token from the order book.
    Returns None when the book is illiquid (spread > ILLIQUID_SPREAD).
    The caller (bot.py) treats None as a signal to use ladder bidding.

    Pass a pre-created client (from get_client()) to reuse credentials
    across multiple calls in the same cycle. If None, a new client is
    created internally (backward-compatible, but slower).
    """
    try:
        if client is None:
            client = get_client()
        book = client.get_order_book(token_id)
        bids, asks = _parse_book_sides(book)
        if bids and asks:
            best_bid = _entry_price(bids[0])
            best_ask = _entry_price(asks[0])
            spread = best_ask - best_bid
            if spread > ILLIQUID_SPREAD:
                logger.debug(
                    f"Illiquid book for {token_id[:16]}...: "
                    f"bid={best_bid} ask={best_ask} spread={spread:.3f} "
                    f"-> ladder-bid candidate"
                )
                return None
            return (best_bid + best_ask) / 2.0
        elif asks:
            return None
        elif bids:
            return _entry_price(bids[0])
        return None
    except Exception as e:
        logger.warning(f"Could not fetch order book for {token_id}: {e}")
        return None


def get_bid_price(token_id: str, client: ClobClient = None) -> float | None:
    """
    Returns the best bid price from the order book.
    Used for sell orders — a FOK sell must be placed at or below the best bid
    to guarantee a fill. Returns None if no bids exist.
    """
    try:
        if client is None:
            client = get_client()
        book = client.get_order_book(token_id)
        bids, _ = _parse_book_sides(book)
        if bids:
            return _entry_price(bids[0])
        return None
    except Exception as e:
        logger.warning(f"Could not fetch bid price for {token_id}: {e}")
        return None


def get_ask_price(token_id: str, client: ClobClient = None) -> float | None:
    """
    Returns the best ask price from the order book.
    Used for buy orders — a FOK buy placed at the ask gets an immediate fill
    if the ask has size. Returns None if no asks exist (route to ladder bidding).
    """
    try:
        if client is None:
            client = get_client()
        book = client.get_order_book(token_id)
        _, asks = _parse_book_sides(book)
        if asks:
            return _entry_price(asks[0])
        return None
    except Exception as e:
        logger.warning(f"Could not fetch ask price for {token_id}: {e}")
        return None


def get_book_snapshot(token_id: str, client: ClobClient = None) -> dict:
    """
    Returns bid, ask, mid, and illiquid flag from a single order book fetch.
    Use this instead of multiple separate price calls to avoid 3× CLOB API
    overhead per market during a scan cycle.

    Returns dict with keys:
        bid: float | None  (best bid price)
        ask: float | None  (best ask price)
        mid: float | None  (bid+ask)/2, or best side if one-sided
        illiquid: bool     (True if spread > ILLIQUID_SPREAD or book is empty)
    """
    try:
        if client is None:
            client = get_client()
        book = client.get_order_book(token_id)
        bids, asks = _parse_book_sides(book)
        bid = _entry_price(bids[0]) if bids else None
        ask = _entry_price(asks[0]) if asks else None
        if bid is not None and ask is not None:
            spread = ask - bid
            illiquid = spread > ILLIQUID_SPREAD
            mid = (bid + ask) / 2.0
        elif ask is not None:
            illiquid = True   # Only asks, no buyers — treat as illiquid
            mid = ask
        elif bid is not None:
            illiquid = False  # Only bids, no asks — can sell but not buy
            mid = bid
        else:
            illiquid = True
            mid = None
        return {"bid": bid, "ask": ask, "mid": mid, "illiquid": illiquid}
    except Exception as e:
        logger.warning(f"Could not fetch order book snapshot for {token_id}: {e}")
        return {"bid": None, "ask": None, "mid": None, "illiquid": True}


def is_book_illiquid(token_id: str, client: ClobClient = None) -> bool:
    """
    Returns True if the order book is illiquid (spread > ILLIQUID_SPREAD,
    or missing bids/asks entirely). Used by bot.py to decide whether to
    route to the ladder-bid path.
    """
    try:
        if client is None:
            client = get_client()
        book = client.get_order_book(token_id)
        bids, asks = _parse_book_sides(book)
        if not bids or not asks:
            return True
        return (_entry_price(asks[0]) - _entry_price(bids[0])) > ILLIQUID_SPREAD
    except Exception:
        return True


# -----------------------------------------------------------------------
# Date and temperature parsing helpers
# -----------------------------------------------------------------------

def _extract_date(text: str) -> str | None:
    current_year = datetime.now().year

    # ISO format
    m = re.search(r'\b(\d{4}-\d{2}-\d{2})\b', text)
    if m:
        return m.group(1)

    month_map = {
        "jan": 1, "january": 1, "feb": 2, "february": 2,
        "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
        "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
        "sep": 9, "september": 9, "oct": 10, "october": 10,
        "nov": 11, "november": 11, "dec": 12, "december": 12,
    }

    m = re.search(
        r'\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|'
        r'jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|'
        r'nov(?:ember)?|dec(?:ember)?)\s+(\d{1,2})(?:[,\s]+(\d{4}))?\b',
        text.lower()
    )
    if m:
        month_str = m.group(1)
        day       = int(m.group(2))
        year      = int(m.group(3)) if m.group(3) else current_year
        month_num = month_map.get(month_str[:3])
        if month_num:
            try:
                return datetime(year, month_num, day).strftime("%Y-%m-%d")
            except ValueError:
                pass

    return None


def _normalize_dashes(text: str) -> str:
    """
    Replaces all Unicode dash variants with ASCII hyphen-minus (U+002D).
    Polymarket API uses en-dash (U+2013) in range questions like "52-53°F"
    which breaks regex patterns that only match ASCII hyphen.
    """
    dash_chars = [
        '\u2013',  # en-dash (confirmed in Polymarket data)
        '\u2014',  # em-dash
        '\u2012',  # figure dash
        '\u2010',  # hyphen (Unicode)
        '\u2011',  # non-breaking hyphen
        '\u2212',  # minus sign
    ]
    for d in dash_chars:
        text = text.replace(d, '-')
    return text


def _extract_temp_bounds(text: str) -> dict | None:
    t = _normalize_dashes(text.lower())

    if "\u00b0f" in t or "fahrenheit" in t or "degrees f" in t:
        unit = "F"
    elif "\u00b0c" in t or "celsius" in t or "degrees c" in t:
        unit = "C"
    else:
        unit = "F"  # Default for US markets

    # Confirmed Polymarket format: "be 60°F or higher" / "60°F or above" -> low=60, high=None
    m = re.search(r'(?:be\s+)?(-?\d+(?:\.\d+)?)\s*°?[fc]?\s+or\s+(?:higher|above)', t)
    if m:
        return {"low": float(m.group(1)), "high": None, "unit": unit}

    # "be 60°F or lower" / "60°F or below" -> low=None, high=60
    m = re.search(r'(?:be\s+)?(-?\d+(?:\.\d+)?)\s*°?[fc]?\s+or\s+(?:lower|below)', t)
    if m:
        return {"low": None, "high": float(m.group(1)), "unit": unit}

    # Range: "between X°F and Y°F" or "between X and Y" (units optional between numbers)
    m = re.search(r'between\s+(\d+(?:\.\d+)?)(?:\s*°?[fc])?\s+and\s+(\d+(?:\.\d+)?)', t)
    if m:
        return {"low": float(m.group(1)), "high": float(m.group(2)), "unit": unit}

    # Range: "X-Y°F" (e.g. "88-89°f" in slug/question) — must come before standalone pattern
    # Now handles all dash types (normalized above) and optional spaces around dash
    m = re.search(r'(?:^|(?<=\s)|(?<=\())(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*°?[fc]', t)
    if m:
        return {"low": float(m.group(1)), "high": float(m.group(2)), "unit": unit}

    # Above/exceed/at least/over X
    m = re.search(r'(?:above|exceed|exceeds|at least|over|higher than)\s+(-?\d+(?:\.\d+)?)', t)
    if m:
        return {"low": float(m.group(1)), "high": None, "unit": unit}

    # Below/under/at most/less than X
    m = re.search(r'(?:below|under|at most|less than|lower than)\s+(-?\d+(?:\.\d+)?)', t)
    if m:
        return {"low": None, "high": float(m.group(1)), "unit": unit}

    # Exact degree: "be X°C" or "be X°F" (Polymarket London format)
    # "Will the highest temperature in London be 12°C?" means bucket [12, 13)
    m = re.search(r'(?:be\s+)(\d+(?:\.\d+)?)\s*°[fc]', t)
    if m:
        val = float(m.group(1))
        return {"low": val, "high": val + 1.0, "unit": unit}

    # Standalone "X°F" fallback — anchor left side so we don't grab the second
    # half of "88-89°f". This is the last resort.
    m = re.search(r'(?<!\d)(-?\d+(?:\.\d+)?)\s*°[fc]', t)
    if m:
        return {"low": float(m.group(1)), "high": None, "unit": unit}

    return None


if __name__ == "__main__":
    print("Fetching weather markets from Polymarket Gamma API...")
    try:
        markets = get_weather_markets()
        print(f"\nFound {len(markets)} weather markets:\n")
        for m in markets[:10]:
            meta = parse_market_metadata(m)
            print(f"  conditionId: {m.get('conditionId')}")
            print(f"  Question:    {m.get('question')}")
            print(f"  Parsed:      {meta}")
            print()
    except Exception as e:
        print(f"ERROR: {e}")
