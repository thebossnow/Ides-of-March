#!/usr/bin/env python3
"""
dashboard.py - Web dashboard for the Polymarket weather bot.

Pulls ALL data live from Polymarket APIs. No local database required.

  - Balance:   CLOB API (authenticated, uses .env credentials)
  - Positions: Data API (public, uses POLYMARKET_FUNDER wallet address)
  - Activity:  Data API (public, P&L curve from trade history)
  - Calibration: Data API activity (forecast % stored in local DB only if available)

Usage:
    python dashboard.py [--port 5001] [--ngrok]

Auth:
    Set DASHBOARD_USER and DASHBOARD_PASS in .env
"""

import os
import time
import functools
import threading
import requests
import sqlite3

from flask import Flask, jsonify, render_template, request, Response

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PORT            = int(os.getenv("DASHBOARD_PORT", 5001))
DASHBOARD_USER  = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASS  = os.getenv("DASHBOARD_PASS", "changeme")
FUNDER          = (os.getenv("POLYMARKET_FUNDER") or "").strip().lower()
PRIVATE_KEY     = os.getenv("POLYMARKET_PRIVATE_KEY", "")
SIG_TYPE        = int(os.getenv("POLYMARKET_SIG_TYPE", "2"))

DATA_API  = "https://data-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID  = 137

CACHE_TTL = 30  # seconds

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Simple thread-safe cache
# ---------------------------------------------------------------------------
_cache: dict = {}
_cache_lock = threading.Lock()


def _cached(key: str, ttl: int, fn):
    """Return cached value if fresh, else call fn(), cache, and return."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (time.time() - entry["ts"]) < ttl:
            return entry["val"]
    val = fn()
    with _cache_lock:
        _cache[key] = {"val": val, "ts": time.time()}
    return val


# ---------------------------------------------------------------------------
# Basic auth
# ---------------------------------------------------------------------------
def _check_auth(u, p):
    return u == DASHBOARD_USER and p == DASHBOARD_PASS


def requires_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not _check_auth(auth.username, auth.password):
            return Response(
                "Authentication required.",
                401,
                {"WWW-Authenticate": 'Basic realm="Polymarket Dashboard"'},
            )
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# CLOB client (lazy, singleton) — only needed for balance
# ---------------------------------------------------------------------------
_clob_client = None
_clob_lock   = threading.Lock()


def _get_clob():
    global _clob_client
    with _clob_lock:
        if _clob_client is None and PRIVATE_KEY and FUNDER:
            try:
                from py_clob_client_v2 import ClobClient
                c = ClobClient(
                    CLOB_HOST,
                    CHAIN_ID,
                    PRIVATE_KEY,
                    signature_type=SIG_TYPE,
                    funder=FUNDER,
                )
                c.set_api_creds(c.create_or_derive_api_key())
                _clob_client = c
            except Exception as e:
                print(f"[dashboard] CLOB client init failed: {e}")
        return _clob_client


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def _fetch_balance() -> dict:
    """USDC balance via CLOB API. Returns {balance, error}."""
    client = _get_clob()
    if not client:
        return {"balance": None, "error": "CLOB client unavailable (check .env)"}

    try:
        from py_clob_client_v2 import BalanceAllowanceParams, AssetType
        for sig in [1, 2]:
            try:
                params = BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=sig,
                )
                result = client.get_balance_allowance(params)
                if isinstance(result, dict) and "balance" in result:
                    bal = int(result["balance"]) / 1_000_000
                    if bal > 0:
                        return {"balance": round(bal, 2), "error": None}
            except Exception:
                continue
        return {"balance": None, "error": "Balance API returned 0 for all sig types"}
    except Exception as e:
        return {"balance": None, "error": str(e)}


def _fetch_positions() -> list:
    """
    Open positions from Polymarket Data API.
    Returns list of position objects with enriched fields.
    """
    if not FUNDER:
        return []
    try:
        url = f"{DATA_API}/positions"
        resp = requests.get(url, params={"user": FUNDER, "sizeThreshold": "0.01"}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return []
        # Normalize fields
        out = []
        for p in data:
            size = float(p.get("size", 0) or 0)
            avg  = float(p.get("avgPrice", 0) or 0)
            cur  = float(p.get("currentValue", 0) or 0)
            init = float(p.get("initialValue", p.get("size", 0) * avg) or 0)
            out.append({
                "token_id":      p.get("asset", ""),
                "condition_id":  p.get("conditionId", ""),
                "title":         p.get("title", ""),
                "outcome":       p.get("outcome", "YES"),
                "shares":        round(size, 4),
                "avg_price":     round(avg, 4),
                "initial_value": round(init, 2),
                "current_value": round(cur, 2),
                "unrealized_pnl": round(cur - init, 2),
                "pct_pnl":       round(p.get("percentPnl", 0) or 0, 4),
                "end_date":      p.get("endDate", ""),
                "market_slug":   p.get("marketSlug", ""),
            })
        return out
    except Exception as e:
        print(f"[dashboard] Positions fetch error: {e}")
        return []


def _fetch_activity() -> list:
    """
    Full trade/activity history from Polymarket Data API.
    Used to build the P&L curve and summary stats.
    Returns raw activity list sorted by timestamp ascending.
    """
    if not FUNDER:
        return []
    try:
        url  = f"{DATA_API}/activity"
        resp = requests.get(url, params={"user": FUNDER, "limit": 1000}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return []
        # Sort ascending by timestamp
        return sorted(data, key=lambda x: x.get("timestamp", "") or "")
    except Exception as e:
        print(f"[dashboard] Activity fetch error: {e}")
        return []


def _build_pnl_curve(activity: list) -> list:
    """
    Computes cumulative P&L over time from activity events.
    BUY  => cash out (-cost)
    SELL => cash in  (+proceeds)
    REDEEM => cash in (+1 per share won)
    Returns list of {time, event_pnl, cumulative, type, title}.
    """
    cumulative = 0.0
    curve = []
    for ev in activity:
        ev_type = (ev.get("type") or "").upper()
        size    = float(ev.get("size", 0) or 0)
        price   = float(ev.get("price", 0) or 0)
        ts      = ev.get("timestamp", "")
        title   = ev.get("title", ev.get("market", ""))

        if ev_type == "BUY":
            event_pnl = -(size * price)       # money out
        elif ev_type in ("SELL", "REDEEM", "REDEEMED"):
            event_pnl = size * price           # money in
        else:
            continue                           # skip non-trade events

        cumulative += event_pnl
        curve.append({
            "time":       ts,
            "event_pnl":  round(event_pnl, 4),
            "cumulative": round(cumulative, 4),
            "type":       ev_type,
            "title":      title,
            "price":      price,
            "size":       size,
        })

    return curve


def _build_summary(positions: list, activity: list, balance: float | None) -> dict:
    """Derives summary stats from live API data."""
    open_count    = len(positions)
    open_exposure = sum(p["initial_value"] for p in positions)
    unrealized_pnl = sum(p["unrealized_pnl"] for p in positions)

    # Closed P&L from activity
    realized_pnl = 0.0
    total_buys   = 0
    total_sells  = 0
    for ev in activity:
        ev_type = (ev.get("type") or "").upper()
        size    = float(ev.get("size", 0) or 0)
        price   = float(ev.get("price", 0) or 0)
        if ev_type == "BUY":
            realized_pnl -= size * price
            total_buys   += 1
        elif ev_type in ("SELL", "REDEEM", "REDEEMED"):
            realized_pnl += size * price
            total_sells  += 1

    return {
        "usdc_balance":    balance,
        "open_count":      open_count,
        "open_exposure":   round(open_exposure, 2),
        "unrealized_pnl":  round(unrealized_pnl, 2),
        "realized_pnl":    round(realized_pnl, 2),
        "total_pnl":       round(realized_pnl + unrealized_pnl, 2),
        "total_buys":      total_buys,
        "total_sells":     total_sells,
        "total_trades":    total_buys + total_sells,
    }


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/api/balance")
@requires_auth
def api_balance():
    data = _cached("balance", CACHE_TTL, _fetch_balance)
    return jsonify(data)


@app.route("/api/positions")
@requires_auth
def api_positions():
    data = _cached("positions", CACHE_TTL, _fetch_positions)
    return jsonify(data)


@app.route("/api/pnl_curve")
@requires_auth
def api_pnl_curve():
    activity = _cached("activity", CACHE_TTL, _fetch_activity)
    curve    = _build_pnl_curve(activity)
    return jsonify(curve)


@app.route("/api/summary")
@requires_auth
def api_summary():
    positions = _cached("positions", CACHE_TTL, _fetch_positions)
    activity  = _cached("activity",  CACHE_TTL, _fetch_activity)
    balance   = _cached("balance",   CACHE_TTL, _fetch_balance).get("balance")
    summary   = _build_summary(positions, activity, balance)
    return jsonify(summary)


@app.route("/api/calibration")
@requires_auth
def api_calibration():
    """Calibration: for each forecast-probability bucket, show actual win rate."""
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "positions.db")
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        cur = con.execute(
            "SELECT forecast_prob, status FROM positions "
            "WHERE status IN ('resolved_won','resolved_lost','redeemed') "
            "AND forecast_prob IS NOT NULL"
        )
        rows = cur.fetchall()
        con.close()
    except Exception as e:
        return jsonify({"error": str(e), "buckets": []}), 500

    bins = [(0.0,0.1),(0.1,0.2),(0.2,0.3),(0.3,0.4),(0.4,0.5),
            (0.5,0.6),(0.6,0.7),(0.7,0.8),(0.8,0.9),(0.9,1.01)]
    buckets = []
    for lo, hi in bins:
        sample = [r for r in rows if lo <= r["forecast_prob"] < hi]
        n = len(sample)
        wins = sum(1 for r in sample if r["status"] in ("resolved_won","redeemed"))
        buckets.append({
            "bucket_low": round(lo, 2),
            "bucket_high": round(min(hi, 1.0), 2),
            "predicted": round((lo + min(hi, 1.0)) / 2.0, 3),
            "actual": round(wins / n, 3) if n else None,
            "count": n,
            "wins": wins,
        })
    n_total = len(rows)
    wins_total = sum(1 for r in rows if r["status"] in ("resolved_won","redeemed"))
    return jsonify({
        "buckets": buckets,
        "total_resolved": n_total,
        "total_wins": wins_total,
        "overall_win_rate": round(wins_total / n_total, 3) if n_total else None,
    })


@app.route("/api/activity")
@requires_auth
def api_activity():
    """Raw activity log (most recent first)."""
    data = _cached("activity", CACHE_TTL, _fetch_activity)
    return jsonify(list(reversed(data)))


@app.route("/")
@requires_auth
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Polymarket Bot Dashboard")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--ngrok", action="store_true")
    args = parser.parse_args()

    if not FUNDER:
        print("[dashboard] WARNING: POLYMARKET_FUNDER not set. Position/activity data will be empty.")
    if not PRIVATE_KEY:
        print("[dashboard] WARNING: POLYMARKET_PRIVATE_KEY not set. Balance will be unavailable.")

    if args.ngrok:
        try:
            from pyngrok import ngrok as pyngrok
            public_url = pyngrok.connect(args.port)
            print(f"\n  ngrok tunnel: {public_url}\n")
        except ImportError:
            print("pyngrok not installed. Run: pip install pyngrok --break-system-packages")
        except Exception as e:
            print(f"ngrok error: {e}")

    print(f"  Funder: {FUNDER or 'NOT SET'}")
    print(f"  Auth:   {DASHBOARD_USER} / {'*' * len(DASHBOARD_PASS)}")
    print(f"  URL:    http://0.0.0.0:{args.port}")
    print()

    app.run(host="0.0.0.0", port=args.port, debug=False)
