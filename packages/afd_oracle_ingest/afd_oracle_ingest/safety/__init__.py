"""Change-safety layer: deterministic detectors that model where a change can hurt, and a gate that applies them.

Built at ingest time from HEAD and history (`step.run_safety`), stored in the per-repo SQLite store, and applied to a
proposed or actual change by `gate.assess_change` / `gate.check_diff` (exposed via MCP, CLI, dashboard and skill).
"""
