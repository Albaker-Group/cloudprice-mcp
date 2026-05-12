"""Historical price snapshots.

Each `YYYY-MM-DD.json` here is the full multi-cloud price catalog as it was on
that date. Written by scripts/refresh_prices.py once a week; never overwritten.

This is the persistence layer for cloudprice-mcp's public price-history
dataset. Loaded via cloudprice_mcp.history.
"""
