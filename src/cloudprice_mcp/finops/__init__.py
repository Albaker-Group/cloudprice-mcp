"""FinOps decision tools (v0.6+).

Each module in this package is a single decision orchestrator over the
existing per-section comparators in cloudprice_mcp.compare:

  migration.py         — assess_migration: should I move workload X from cloud A?
  commitment.py        — optimize_commitment: when does my RI / SP / CUD pay back?
  tco.py               — compare_total_cost_of_ownership: 3-year cost across clouds
  egress_arbitrage.py  — find_egress_arbitrage: where do I save on data transfer?

All four consume the canonical WorkloadInventory dataclass from inventory.py
and produce a structured result dict consumable by export.py renderers.
"""
