#!/bin/bash
# deploy.sh - VPS setup and bot deployment script
# Run this ONCE on your VPS after uploading the bot files.
# Usage: bash deploy.sh

set -e

echo "=== Polymarket Weather Bot VPS Setup ==="
echo ""

# 1. Update system
echo "[1/7] Updating system packages..."
sudo apt-get update -q && sudo apt-get upgrade -y -q

# 2. Install Python, pip, screen, git
echo "[2/7] Installing Python 3, pip, screen, git..."
sudo apt-get install -y -q python3 python3-pip python3-venv screen git ufw

PYTHON_VERSION=$(python3 --version 2>&1)
echo "  Python: $PYTHON_VERSION"

# 3. Firewall
echo "[3/7] Configuring firewall..."
sudo ufw allow OpenSSH
sudo ufw --force enable
echo "  Firewall: SSH allowed"

# 4. Virtual environment
echo "[4/7] Creating Python virtual environment..."
python3 -m venv venv
source venv/bin/activate

# 5. Install packages
echo "[5/7] Installing Python packages..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

python3 -c "from py_clob_client.client import ClobClient; print('  py-clob-client: OK')"
python3 -c "import requests; print('  requests: OK')"
python3 -c "import schedule; print('  schedule: OK')"
python3 -c "import web3; print('  web3: OK')"

# 6. Verify .env
echo "[6/7] Checking credentials..."
if [ ! -f ".env" ]; then
    echo ""
    echo "  .env not found. Creating it now..."
    echo "  Enter your credentials (input is hidden for security):"
    echo ""
    read -s -p "  POLYMARKET_PRIVATE_KEY (0x...): " PK
    echo ""
    read -s -p "  POLYMARKET_FUNDER (0x... proxy wallet): " FUNDER
    echo ""
    echo "POLYMARKET_PRIVATE_KEY=${PK}" > .env
    echo "POLYMARKET_FUNDER=${FUNDER}" >> .env
    chmod 600 .env
    echo "  .env created with restricted permissions (600)"
else
    chmod 600 .env
    echo "  .env found. Permissions set to 600."
fi

# 7. Import test
echo "[7/7] Running import test..."
python3 -c "
from weather import get_forecast
from markets import get_weather_markets
from strategy import forecast_probability, kelly_position_size
from executor import DRY_RUN
from logger import log_scan
print('  All modules import successfully')
print(f'  DRY_RUN = {DRY_RUN}')
"

echo ""
echo "=== Setup complete! ==="
echo ""
echo "BEFORE TRADING - run the one-time allowance setup:"
echo "  source venv/bin/activate"
echo "  python3 set_allowances.py"
echo ""
echo "Then test each module:"
echo "  python3 weather.py        # Test Open-Meteo API"
echo "  python3 markets.py        # Test market discovery"
echo ""
echo "Start bot in paper trading mode:"
echo ""
echo "  screen -S weatherbot"
echo "  source venv/bin/activate"
echo "  python3 bot.py"
echo "  [Ctrl+A then D to detach]"
echo ""
echo "Monitor:"
echo "  tail -f bot.log"
echo "  tail -f scan_log.csv"
echo ""
echo "  REMINDER: DRY_RUN=True by default."
echo "  Paper trade 7+ days before setting DRY_RUN=False."
echo ""
