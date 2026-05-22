"""
CLI entry point for backtesting.

Usage:
    python scripts/run_backtest.py \\
        --csv data/historical/MNQ_1m_2024.csv \\
        --config configs/config.yaml \\
        --start 2024-01-01 \\
        --end   2024-12-31
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtesting.runner import main

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LSM-v2 Backtest")
    parser.add_argument("--csv",    required=True,  help="Path to 1m OHLCV CSV")
    parser.add_argument("--config", required=True,  help="Path to config.yaml")
    parser.add_argument("--start",  default=None,   help="Start date YYYY-MM-DD")
    parser.add_argument("--end",    default=None,   help="End date YYYY-MM-DD")
    args = parser.parse_args()

    asyncio.run(main(args.csv, args.config, args.start, args.end))
