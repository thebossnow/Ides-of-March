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
from py_clob_client_v2 import ClobClient  # type hints only
from executor import get_client  # Use singleton — avoids repeated auth derivation

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
    # Additional cities
    "amsterdam":        "Amsterdam",
    "helsinki":          "Helsinki",
    "panama city":      "Panama City",
    "kuala lumpur":     "Kuala Lumpur",
    "jakarta":          "Jakarta",
    # Additional cities added 2026-04-19
    "busan":            "Busan",
    "cape town":        "Cape Town",
    "guangzhou":        "Guangzhou",
    "jeddah":           "Jeddah",
    "karachi":          "Karachi",
    "lagos":            "Lagos",
    "manila":           "Manila",
}




def get_weather_markets() -> list:
    """
    Fetches active temperature markets using the Gamma API events endpoint.

    The old approach queried /series first, but that endpoint only returns 2
    series (Dallas and Hong Kong) even though 50+ city series exist. The root
    cause: the /series endpoint silently omits series that have the
    'hide-from-new' tag, which all current city weather series carry.

    Correct approach:
      - Query /events?tag_slug=daily-temperature&order=endDate&ascending=false
        This returns all city-day events newest-first so open events come first
        without paginating through thousands of historical closed events.
      - Stop as soon as the API returns a page containing only closed events —
        since results are sorted newest-first this is safe and fast.
      - Events embed full market objects, no extra calls needed.
    """
    import requests

    GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

    all_markets = []
    seen_slugs: set = set()
    city_series: set = set()
    offset = 0

    while True:
        try:
            r = requests.get(
                GAMMA_EVENTS_URL,
                params={
                    "tag_slug":  "daily-temperature",
                    "order":     "endDate",
                    "ascending": "false",
                    "limit":     100,
                    "offset":    offset,
                },
                timeout=15,
            )
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            logger.error(f"Failed to fetch temperature events (offset={offset}): {e}")
            break

        if not isinstance(batch, list) or not batch:
            break

        open_in_batch = 0
        for event in batch:
            if event.get("closed"):
                continue
            open_in_batch += 1
            series_slug = event.get("seriesSlug", "")
            if series_slug:
                city_series.add(series_slug)
            for m in event.get("markets", []):
                slug = m.get("slug", "")
                if slug and slug not in seen_slugs and m.get("active") and not m.get("closed"):
                    all_markets.append(m)
                    seen_slugs.add(slug)

        # Sorted newest-first: once an entire page is closed, all subsequent
        # pages will also be closed — stop paginating.
        if open_in_batch == 0:
            break

        if len(batch) < 100:
            break
        offset += 100

    logger.info(
        f"Found {len(all_markets)} active weather markets across "
        f"{len(city_series)} city series"
    )
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


def get_no_token_id(market: dict) -> str | None:
    """
    Returns the tokenId for the NO outcome of a binary market.
    Mirror of get_yes_token_id: matches outcome label first, then falls back
    to clobTokenIds[1]. Used by ctf.py split/merge bookkeeping (need both
    legs to track inventory and merge complementary pairs).
    """
    token_ids = _parse_json_field(market.get("clobTokenIds", []))
    outcomes  = _parse_json_field(market.get("outcomes", []))

    for i, outcome in enumerate(outcomes):
        if outcome.upper() in ("NO", "FALSE") and i < len(token_ids):
            return str(token_ids[i])

    if len(token_ids) >= 2:
        return str(token_ids[1])
    return None


def get_neg_risk_flag(market: dict) -> bool:
    """
    Detects whether a market uses the NegRiskAdapter for split/merge/redeem.

    Polymarket fields are inconsistent across API surfaces:
      - Gamma API:    "negRisk" (camelCase)
      - CLOB API:     "neg_risk" (snake)
      - WS new_market events: usually "negRisk"
    We accept any of them, defaulting to False.
    """
    for key in ("negRisk", "neg_risk", "negRiskMarket"):
        if key in market:
            return bool(market.get(key))
    return False


def parse_market_metadata(market: dict) -> dict | None:
    """
    Attempts to extract city, date, and temperature bucket from a market.
    Tries the human-readable question first, then falls back to parsing
    the slug (which has a deterministic format on Polymarket).
    Returns a metadata dict or None if unparseable.

    The returned dict's "market_type" field is "highest" or "lowest".
    Callers must route "lowest" markets to get_forecast_low() (daily MIN)
    and "highest" markets to get_forecast() (daily MAX).
    """
    question     = market.get("question", "")
    slug         = market.get("slug", "")
    condition_id = market.get("conditionId", "")

    # Detect market type (highest-temp = daily MAX, lowest-temp = daily MIN).
    s_lower = slug.lower()
    q_lower = question.lower()
    if "lowest-temperature" in s_lower or "lowest temperature" in q_lower:
        market_type = "lowest"
    else:
        market_type = "highest"

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

    # Permanent fix for unrealistic bucket definitions (e.g. '19-19', zero-width, or malformed ranges)
    # Ensure all closed buckets are at least 1 degree wide. This matches Polymarket's actual 1° increments.
    b = bounds
    if b.get("low") is not None and b.get("high") is not None and b["high"] - b["low"] < 0.1:
        logger.warning(f"Zero-width bucket normalized {b['low']}-{b['high']} → {b['low']}-{b['low']+1.0} for {slug}")
        b["high"] = b["low"] + 1.0

    yes_token_id = get_yes_token_id(market)
    no_token_id  = get_no_token_id(market)
    neg_risk     = get_neg_risk_flag(market)

    return {
        "city":          city_key,
        "date":          date_str,
        "bucket_low":    bounds["low"],
        "bucket_high":   bounds["high"],
        "unit":          bounds["unit"],
        "question":      question,
        "condition_id":  condition_id,
        "yes_token_id":  yes_token_id,
        "no_token_id":   no_token_id,
        "neg_risk":      neg_risk,
        "market_type":   market_type,
        "slug":          slug,
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

    # Must be a weather market slug (highest OR lowest temperature)
    if s.startswith("highest-temperature-in-"):
        rest = s[len("highest-temperature-in-"):]
    elif s.startswith("lowest-temperature-in-"):
        rest = s[len("lowest-temperature-in-"):]
    else:
        return None
    # e.g. "chengdu-on-april-3-2026-20corbelow"

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
    Examples: "20corbelow", "84forhigher", "52-53f", "10c", "neg-1c", "neg-3corbelow"

    Polymarket encodes negative temps as "neg-X" in slugs (e.g. "neg-1c" = -1°C).
    """
    t = temp_part.strip()

    # Normalize "neg-" prefix to actual negative sign for parsing.
    # "neg-1c" -> "-1c", "neg-3corbelow" -> "-3corbelow"
    if t.startswith("neg-"):
        t = "-" + t[4:]

    # Range: "52-53f" or "52-53c" (only positive ranges exist on Polymarket)
    m = re.match(r'^(\d+)-(\d+)([fc])$', t)
    if m:
        unit = "F" if m.group(3) == "f" else "C"
        return {"low": float(m.group(1)), "high": float(m.group(2)), "unit": unit}

    # "Xforbelow" or "Xcorbelow" (or below) - supports negative temps
    m = re.match(r'^(-?\d+)([fc])orbelow$', t)
    if m:
        unit = "F" if m.group(2) == "f" else "C"
        return {"low": None, "high": float(m.group(1)), "unit": unit}

    # "Xforhigher" or "Xcorhigher" (or higher) - supports negative temps
    m = re.match(r'^(-?\d+)([fc])orhigher$', t)
    if m:
        unit = "F" if m.group(2) == "f" else "C"
        return {"low": float(m.group(1)), "high": None, "unit": unit}

    # Exact degree: "10c", "65f", "-1c" (bucket = [X, X+1))
    # Also fixes unrealistic zero-width buckets like "19-19" reported by user
    m = re.match(r'^(-?\d+)([fc])$', t)
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
# get routed to the book-sweep / last-trade-price path instead of the midpoint FOK.
# Prices are probabilities (0-1), so 0.50 was far too permissive -- it let markets
# with bid=$0.01 / ask=$0.51 pass as "liquid". 0.15 means a market with a bid of
# $0.30 and ask of $0.45 (spread $0.15) is still considered liquid; anything wider
# routes to the book-sweep execution path.
ILLIQUID_SPREAD = 0.15


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
        bids = book.get('bids', []) if isinstance(book, dict) else (book.bids or [])
        asks = book.get('asks', []) if isinstance(book, dict) else (book.asks or [])
        if bids and asks:
            best_bid = float(bids[0]['price'] if isinstance(bids[0], dict) else bids[0].price)
            best_ask = float(asks[0]['price'] if isinstance(asks[0], dict) else asks[0].price)
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
            # Only asks, no bids at all -> illiquid
            return None
        elif bids:
            # Only bids, no asks -> no sellers to match a FOK buy against.
            # Treat as illiquid so the caller routes to ladder bids.
            logger.debug(
                f"Bids-only book for {token_id[:16]}...: "
                f"best_bid={float(bids[0].price)} no asks -> illiquid"
            )
            return None
        return None
    except Exception as e:
        logger.warning(f"Could not fetch order book for {token_id}: {e}")
        return None


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
        bids = book.get('bids', []) if isinstance(book, dict) else (book.bids or [])
        asks = book.get('asks', []) if isinstance(book, dict) else (book.asks or [])
        if not bids or not asks:
            return True
        best_bid = float(bids[0].price)
        best_ask = float(asks[0].price)
        return (best_ask - best_bid) > ILLIQUID_SPREAD
    except Exception:
        return True


def get_book_asks(token_id: str, client: ClobClient = None) -> list[tuple[float, float]]:
    """
    Returns the ask side of the order book as sorted (price, size) tuples.

    price = limit price as probability (0.01-0.99)
    size  = number of outcome token shares available at that price

    Sorted ascending by price so callers can walk from cheapest ask upward,
    buying only levels where edge remains positive against the forecast prob.
    Returns an empty list if the book has no asks or on any API failure.

    Args:
        token_id: outcome token ID
        client:   optional pre-authenticated ClobClient (reuse across calls)

    Returns:
        List of (price, size) tuples sorted by price ascending.
    """
    try:
        if client is None:
            client = get_client()
        book = client.get_order_book(token_id)
        asks = book.get('asks', []) if isinstance(book, dict) else (book.asks or [])
        result = []
        for ask in asks:
            try:
                p = ask['price'] if isinstance(ask, dict) else ask.price
                s = ask['size'] if isinstance(ask, dict) else ask.size
                result.append((float(p), float(s)))
            except (AttributeError, ValueError, TypeError):
                continue
        return sorted(result, key=lambda x: x[0])
    except Exception as e:
        logger.warning(f"Could not fetch order book asks for {token_id}: {e}")
        return []


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
    # Supports negative temps: "be -1°C" -> bucket [-1, 0)
    # Must NOT match "be X°C or higher/lower" (those are caught above).
    m = re.search(r'(?:be\s+)(-?\d+(?:\.\d+)?)\s*°[fc](?!\s+or\s)', t)
    if m:
        val = float(m.group(1))
        return {"low": val, "high": val + 1.0, "unit": unit}

    # Standalone "X°F" fallback -- anchor left side so we don't grab the second
    # half of "88-89°f". This is the last resort.
    # Note: this returns open-ended (low=X, high=None) because without
    # "or higher/lower" context we cannot determine the bucket type.
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
