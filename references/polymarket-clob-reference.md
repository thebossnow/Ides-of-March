# Polymarket CLOB & Gamma API Reference

> Condensed from the full docs mirror (`github.com/thebossnow/polymarket-llm-wiki`).
> Covers only the endpoints this bot actually uses. Captured 2026-06-21.

---

## Base URLs

| API | URL |
|-----|-----|
| CLOB (trading) | `https://clob.polymarket.com` |
| Gamma (market data) | `https://gamma-api.polymarket.com` |
| CLOB staging | `https://clob-staging.polymarket.com` |

---

## Rate Limits

All enforced via Cloudflare throttling (delayed, not rejected).

### Gamma API

| Endpoint | Limit |
|---|---|
| General | 4,000 req / 10s |
| `/events` | 500 req / 10s |
| `/markets` | 300 req / 10s |
| `/markets` + `/events` combined | 900 req / 10s |

### CLOB API — Market Data

| Endpoint | Limit |
|---|---|
| General | 9,000 req / 10s |
| `/book` | 1,500 req / 10s |
| `/price` / `/midpoint` | 1,500 req / 10s |
| `/books` / `/prices` / `/midpoints` (batch) | 500 req / 10s |
| `/prices-history` | 1,000 req / 10s |
| Market tick size | 200 req / 10s |

### CLOB API — Trading

| Endpoint | Burst | Sustained |
|---|---|---|
| `POST /order` | 5,000 req / 10s | 120,000 req / 10 min |
| `DELETE /order` | 5,000 req / 10s | 120,000 req / 10 min |
| `DELETE /cancel-all` | 250 req / 10s | 6,000 req / 10 min |
| `/trades`, `/orders`, `/order` (read) | 900 req / 10s | — |

---

## Authentication

### Public (No Auth)

- Gamma API — all endpoints (markets, events, tags, comments)
- CLOB read endpoints — `/book`, `/price`, `/prices`, `/midpoint`, `/midpoints`, `/tick-size`, `/fee-rate`, `/prices-history`
- Data API — `/trades`, `/positions`, `/closed-positions`
- No API key or headers needed.

### L2 Authenticated (CLOB Trading)

Required for: `POST /order`, `DELETE /order`, `DELETE /cancel-all`, `POST /heartbeats`.

Five HTTP headers:

| Header | Description |
|---|---|
| `POLY_API_KEY` | API key (UUID from L1 derivation) |
| `POLY_PASSPHRASE` | Passphrase from L1 derivation |
| `POLY_ADDRESS` | Polygon signer address |
| `POLY_SIGNATURE` | HMAC-SHA256 of `{method}{path}{timestamp}{body}` using `secret` |
| `POLY_TIMESTAMP` | Current UNIX timestamp (string) |

### Signature Types

| Type | Value | When |
|---|---|---|
| EOA | `0` | Direct wallet — pays own POL gas |
| POLY_PROXY | `1` | Legacy Magic Link proxy wallet |
| GNOSIS_SAFE | `2` | Safe wallet |
| POLY_1271 | `3` | Deposit wallet (recommended for new API users) |

---

## Endpoints Used by the Bot

### GET `/events` (Gamma, public)

Filter weather markets by tag slug:

```bash
curl "https://gamma-api.polymarket.com/events?tag_slug=daily-temperature&active=true&closed=false&limit=100"
```

**Important:** The `slug_url` parameter on `/markets` is BROKEN — it silently returns random non-weather markets. Always use `/events?tag_slug=daily-temperature` for weather market discovery.

**Key parameters on `/events`:**

| Parameter | Type | Description |
|---|---|---|
| `tag_slug` | string | Filter by tag slug (e.g. `daily-temperature`) |
| `active` | bool | Live tradable events |
| `closed` | bool | Default `false` — omit unless you want history |
| `limit` | int | Results per page |
| `offset` | int | Pagination offset |
| `order` | string | Sort field: `volume_24hr`, `volume`, `liquidity`, `start_date`, `end_date` |
| `ascending` | bool | Sort direction (default `false`) |
| `slug` | string[] | Filter by specific event slug(s) |

**Response schema** (relevant fields on the Event object):

| Field | Type | Description |
|---|---|---|
| `id` | string | Event ID |
| `slug` | string | Event slug |
| `title` | string | Event title |
| `active` | bool | Whether event is active |
| `closed` | bool | Whether event is closed |
| `enableOrderBook` | bool | Whether CLOB trading is enabled |
| `negRisk` | bool | Whether negative risk is enabled |
| `markets` | array | Associated markets (see Market schema below) |
| `startDate` | datetime | Event start |
| `endDate` | datetime | Event end / market close |

### GET `/markets/slug/{slug}` (Gamma, public)

Fetch a specific market by slug:

```bash
curl "https://gamma-api.polymarket.com/markets/slug/tokyo-will-the-high-temperature-reach-24-c-on-may-17"
```

**Key fields on Market:**

| Field | Type | Description |
|---|---|---|
| `id` | string | Market ID |
| `conditionId` | string | Condition ID (CTF contract identifier) |
| `question` | string | Market question |
| `slug` | string | Market slug |
| `clobTokenIds` | string | JSON array: `[yesTokenId, noTokenId]` |
| `outcomePrices` | string | JSON array: `[yesPrice, noPrice]` |
| `endDate` | datetime | Market end / resolution date |
| `enableOrderBook` | bool | CLOB tradable? |
| `negRisk` | bool | Negative risk enabled? |
| `minimumTickSize` | number | Price tick increment |
| `rfqEnabled` | bool | RFQ quotes enabled? |
| `makerBaseFee` | int | Maker fee in bips |
| `takerBaseFee` | int | Taker fee in bips |

### GET `/book` (CLOB, public)

Fetch order book for a token ID:

```bash
curl "https://clob.polymarket.com/book?token_id=0xabc123def456..."
```

**Response:**

```json
{
  "market": "0xcondID...",
  "asset_id": "0xtokenID...",
  "timestamp": "1234567890",
  "hash": "a1b2c3d4...",
  "bids": [{"price": "0.45", "size": "100"}, ...],
  "asks": [{"price": "0.46", "size": "150"}, ...],
  "min_order_size": "1",
  "tick_size": "0.01",
  "neg_risk": false,
  "last_trade_price": "0.46"
}
```

> **Note:** Response is a **dict**, not an object with `.asks`/.`bids` properties. Your bot already handles both via `_parse_book_sides()`.

### POST `/order` (CLOB, L2 auth)

Place a signed order. Requires all 5 POLY_* L2 headers.

**Request body:**

```json
{
  "order": {
    "maker": "0xfunderAddr",
    "signer": "0xsignerAddr",
    "tokenId": "0xtokenID",
    "makerAmount": "100000000",
    "takerAmount": "200000000",
    "side": "BUY",
    "expiration": "1735689600",
    "timestamp": "1735689600000",
    "metadata": "",
    "builder": "0x0000...0000",
    "signature": "0x1234...",
    "salt": 1234567890,
    "signatureType": 0
  },
  "owner": "api-key-uuid",
  "orderType": "GTC",
  "deferExec": false,
  "postOnly": false
}
```

**Response:**

```json
{
  "success": true,
  "orderID": "0xabcdef...",
  "status": "live",
  "makingAmount": "100000000",
  "takingAmount": "200000000",
  "errorMsg": ""
}
```

**Status values:** `live`, `matched`, `delayed`
**Order types:** `GTC` (default), `FOK`, `GTD`, `FAK`

### DELETE `/order` (CLOB, L2 auth)

Cancel a single order:

```json
{"orderID": "0xabcdef..."}
```

Response: `{"canceled": ["0xabc..."], "not_canceled": {}}`

### DELETE `/cancel-all` (CLOB, L2 auth)

Cancel ALL open orders for the authenticated user. Works in cancel-only mode.

Response: `{"canceled": ["0xabc..."], "not_canceled": {}}`

### POST `/heartbeats` (CLOB, L2 auth)

Send heartbeat to maintain session. If not sent regularly, all open orders are **automatically canceled**. Useful as a dead-man's switch.

Response: `{"status": "ok"}`

### GET `/data/order/{orderID}` (CLOB, L2 auth)

Check status of a specific order:

```json
{
  "id": "0xabcdef...",
  "status": "ORDER_STATUS_LIVE",
  "maker_address": "0x...",
  "market": "0xconditionID",
  "asset_id": "0xtokenID",
  "side": "BUY",
  "original_size": "100000000",
  "size_matched": "0",
  "price": "0.5",
  "outcome": "YES",
  "expiration": "1735689600",
  "order_type": "GTC",
  "created_at": 1700000000
}
```

**Order statuses:** `ORDER_STATUS_LIVE`, `ORDER_STATUS_INVALID`, `ORDER_STATUS_CANCELED_MARKET_RESOLVED`, `ORDER_STATUS_CANCELED`, `ORDER_STATUS_MATCHED`

---

## Matching Engine Restarts

### How to detect

During a restart, the CLOB returns **HTTP 425 (Too Early)** on all order endpoints. After the restart, the engine enters **post-only mode for 2 minutes**.

### Recommended retry logic

```python
import time, requests

def post_with_retry(path, body, headers, max_retries=10):
    delay = 1
    for attempt in range(max_retries):
        resp = requests.post(f"https://clob.polymarket.com{path}",
                             json=body, headers=headers)
        if resp.status_code == 425:
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        return resp
    raise Exception("Engine restart exceeded max retries")
```

### Post-only mode (503)

During the 2-min window, `POST /order` returns:

```json
{
  "error": "post-only mode: only post-only orders and cancels are allowed",
  "code": "post_only_mode",
  "retry_after_seconds": 79
}
```

Only cancels and orders with `postOnly: true` are accepted.

### Cancel-only mode (503)

```json
{
  "error": "Trading is currently cancel-only. New orders are not accepted, but cancels are allowed."
}
```

### Announcement channels

- Telegram: [t.me/polytradingapis](https://t.me/polytradingapis)
- Discord: #trading-apis in Polymarket server

---

## Error Codes

| Error | Meaning |
|---|---|
| `Invalid API key` | L2 auth failed |
| `Invalid order payload` | Malformed order body |
| `owner mismatch` | `owner` UUID doesn't match API key |
| `signer mismatch` | `signer` address doesn't match API key |
| `{address} address banned` | Address restricted |
| `{address} in closed only mode` | Address in cancel-only |
| `No orderbook exists for {token_id}` | Token not found or not CLOB-tradable |
| `price breaks minimum tick size` | Price not aligned to tick size |
| `post_only_mode` | Engine in post-only mode, `retry_after_seconds` provided |
| `Invalid orderID` | Order hash not found or malformed |

---

## Conditional Token Framework (CTF)

Relevant for on-chain resolution/redeem operations (handled by `resolver.py`):

| Action | Description |
|---|---|
| **Split** | Convert pUSD into Yes/No outcome token pair |
| **Merge** | Convert outcome token pair back to pUSD |
| **Redeem** | Exchange winning tokens for pUSD after resolution |

Contracts: [resources/contracts.md](https://github.com/thebossnow/polymarket-llm-wiki/blob/main/resources/contracts.md)

---

## Key Gotchas (Learned the Hard Way)

1. **`/events?tag_slug=daily-temperature`** — DO NOT use `/markets?slug_url=...` to filter weather markets. It silently returns garbage.
2. **`/book` returns a dict, not an object** — Access `response["asks"]`, not `response.asks`. Your bot's `_parse_book_sides()` handles this.
3. **Heartbeat session is per-wallet, not per-bot** — Two bots sharing the same wallet cause heartbeat ID collisions (HTTP 400).
4. **`DELETE /cancel-all` works in cancel-only mode** — always available for emergency exit.
5. **Gamma API `condition_id` filter is unreliable** — verify the `conditionId` field in each response matches what you queried.
