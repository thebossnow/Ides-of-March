#!/usr/bin/env python3
"""
calibrate.py - Phase 5 Calibration & Backtesting

Loads from positions and wu_positions.
Computes basic per-city stats from resolved trades with actuals.
Suggests sigma adjustments.
Leverages existing infrastructure (backfill, sigma_calibration_v2, wu_empirical).
"""
import argparse
import sqlite3
from collections import defaultdict
from datetime import datetime
from statistics import mean
from typing import Dict, List

DB_PATH = "positions.db"

def load_resolved(db: str) -> List[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
