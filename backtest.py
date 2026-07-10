"""Thin shim — the backtest engine lives in the shared package (./engine).
Keeps `python3 backtest.py ...` working for existing workflows/skills."""
from rhengine.backtest import main

if __name__ == "__main__":
    main()
