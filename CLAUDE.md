# Project notes for Claude

- This repository is Oracle (`afd-oracle`), an Agentic Fever Dream tool.
  - Python packages: `packages/afd_oracle_*`.
  - Dashboard UI: `packages/afd_oracle_ui/web`.
  - Internal mining machinery: `.claude/agents` and `.claude/workflows`.
- Oracle analysis and verification use the `oracle-analyst` / `oracle-verifier` agents, through the `oracle-*`
  workflows. Do not spawn general-purpose agents for them. If the `afd-oracle` MCP server is not connected, reconnect
  with /mcp instead of working around it.
- User-facing entry points are the dashboard (`uv run afd-oracle ui`), the `afd-oracle` skill, and `/afd oracle
  <action>`. Templates for the last two live in `packages/afd_oracle_ui/afd_oracle_ui/templates/`. Don't add
  project-level slash commands.
