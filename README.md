# Polymarket Weather Bot

An automated trading bot that identifies pricing inefficiencies in Polymarket's temperature prediction markets. It compares weather forecast probabilities against market prices and places trades on underpriced outcomes via Polymarket's CLOB API on Polygon.

**Version:** 2.0.0
**Author:** TheBossNow

---

## Features

- Full position lifecycle: entry, monitoring, exit, resolution, and gasless on-chain redemption
- Student's t-distribution probability model with horizon-dependent uncertainty
- Fractional Kelly criterion position sizing
- Dual-gate trade filtering (edge threshold + dynamic probability floor)
- Same-day loss-cutting and intelligent profit-taking
- 39 cities across 6 continents
- Multi-schedule architecture (30-min scan, 15-min fast monitor, daily resolution, daily report)
- Real-time Telegram alerts and daily P&L reports
- Dynamic bankroll from on-chain USDC balance
- SQLite-backed persistent position tracking (survives restarts)
- DRY_RUN mode for paper trading

## Architecture

```
+------------------+     +------------------+     +------------------+
|   Open-Meteo     |     | Polymarket Gamma |     | Polymarket CLOB  |
|  Forecast API    |     |   (market list)  |     |  (prices/orders) |
+--------+---------+     +--------+---------+     +--------+---------+
         |                        |                    |         ^
         v                        v                    v         |
   +-----------+          +--------------+       +--------------+
   | weather.py|          |  markets.py  |       |  executor.py |
   +-----------+          +--------------+       +--------------+
         |                        |                    ^
         v                        v                    |
   +----------------------------------------------------+
   |              bot.py (Orchestrator)                  |
   +------+---------+---------+---------+---------------+
          |         |         |         |
          v         v         v         v
   +----------+ +--------+ +--------+ +-----------+
   |strategy.py| |logger.py| |notifier| |positions.py|
   +----------+ +--------+ |  .py   | | (SQLite)   |
                            +--------+ +-----+-----+
                                              |
          +-----------------------------------+-------------------+
          v                    v                                   v
   +----------------+  +-------------------+             +-----------+
   |observed_temps.py|  |position_monitor.py|             |redeemer.py|
   +----------------+  +-------------------+             +-----------+
```

## Quick Start

### Prerequisites

- Python 3.10+
- A Polymarket account with a funded proxy wallet
- A Polygon wallet private key (exported from Magic.link)
- Telegram bot token and chat ID (for notifications)
- Builder API credentials (for gasless redemption)

### Setup

```bash
# Clone the repo
git clone https://github.com/TheBossNow/Ides_of_March.git
cd Ides_of_March

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure credentials
cp .env.template .env
# Edit .env with your keys (see Configuration below)
chmod 600 .env

# Run in paper trading mode (DRY_RUN=True by default)
python3 bot.py
```

### Test Individual Modules

Each module can be run standalone to verify setup:

```bash
python3 weather.py          # Test Open-Meteo API
python3 markets.py          # Test market discovery
python3 strategy.py         # Test probability and sizing
python3 positions.py        # Test SQLite lifecycle
python3 observed_temps.py   # Test historical temp fetching
python3 debug_balance.py    # Test on-chain balance
```

### VPS Deployment

```bash
bash deploy.sh              # Automated VPS setup
python3 set_allowances.py   # One-time USDC.e approval (requires POL gas)
screen -S weatherbot
python3 bot.py              # Ctrl+A, D to detach
```

## Configuration

All credentials go in `.env` (never committed to git).

| Variable | Description |
|---|---|
| `POLYMARKET_PRIVATE_KEY` | Hex private key (0x-prefixed) |
| `POLYMARKET_FUNDER` | Proxy wallet address |
| `POLYMARKET_SIG_TYPE` | 1 (EOA) or 2 (Gnosis Safe proxy, default) |
| `TELEGRAM_TOKEN` | Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | Chat ID for notifications |
| `POLYMARKET_BUILDER_KEY` | Builder API key (for redemption) |
| `POLYMARKET_BUILDER_SECRET` | Builder API secret |
| `POLYMARKET_BUILDER_PASSPHRASE` | Builder API passphrase |
| `POSITIONS_DB_PATH` | Optional: custom SQLite path |

### Trading Parameters

These are set as constants in `strategy.py` and `bot.py`:

| Parameter | Default | Description |
|---|---|---|
| Entry threshold | 15% edge | Minimum edge to open a trade |
| Probability floor | 15-30% | Dynamic by forecast horizon |
| Kelly fraction | 15% | Conservative fractional Kelly |
| Position size | $5 to $25 | Per-trade limits |
| Daily spend cap | 20% of bankroll | Maximum daily deployment |
| Scan interval | 30 min | Time between full scan cycles |

## Documentation

See [DEVELOPER_GUIDE_v2.md](DEVELOPER_GUIDE_v2.md) for full technical documentation including module details, data flow, algorithms, API reference, error handling, and glossary.

## DRY_RUN Mode

The bot starts in paper trading mode by default (`DRY_RUN = True` in `executor.py`). In this mode the full pipeline runs (market discovery, forecasts, probability calculation, edge detection, position sizing) but no real orders are placed. Set `DRY_RUN = False` in `executor.py` to enable live trading.

---

## Warnings

**Financial Risk.** This bot trades real money on prediction markets. You can lose some or all of your capital. Past performance does not guarantee future results. The probability model is an approximation and weather forecasts are inherently uncertain. Do not trade with funds you cannot afford to lose.

**No Warranty.** This software is provided "as is" without warranty of any kind, express or implied. The author is not responsible for any financial losses, missed trades, bugs, API changes, or other issues arising from the use of this software.

**Not Financial Advice.** Nothing in this repository constitutes financial, investment, or trading advice. The author is not a licensed financial advisor. You are solely responsible for your own trading decisions.

**API and Protocol Risk.** This bot depends on third-party services (Polymarket, Open-Meteo, Polygon, Telegram). Any of these may change their APIs, terms of service, or availability without notice. Smart contract interactions carry inherent risks including but not limited to bugs, exploits, and loss of funds.

**Security.** Your private key controls your funds. Never share it. Never commit `.env` to version control. Use a dedicated wallet with limited funds. The author is not responsible for compromised credentials.

## Legal Obligations

**You are solely responsible for complying with all applicable laws and regulations in your jurisdiction.** This includes but is not limited to:

- **Prediction market legality.** Prediction markets may be restricted, regulated, or prohibited in your country, state, or territory. It is your responsibility to determine whether using Polymarket is legal where you reside.
- **Tax obligations.** Trading profits may be subject to income tax, capital gains tax, or other tax obligations. You are responsible for reporting and paying all applicable taxes.
- **Sanctions and export controls.** Do not use this software if you are located in or a resident of a jurisdiction subject to U.S. or international sanctions.
- **Terms of service.** You must comply with the terms of service of all platforms and APIs used by this bot (Polymarket, Open-Meteo, Polygon, Telegram). Automated trading may be subject to additional restrictions.
- **Age requirements.** You must meet all minimum age requirements for prediction market participation in your jurisdiction.

**The author does not endorse or encourage any illegal activity. Use of this software in violation of applicable law is strictly at your own risk.**

---

## License

This project is private and not licensed for redistribution.
