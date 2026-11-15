---
description: Agentic Fever Dream tools. Usage /afd <tool> <action> [args], e.g. /afd oracle ask why does this module exist
argument-hint: <tool> <action> [args]
---
<!-- afd router v1. Identical in every Agentic Fever Dream tool; installing another tool overwrites it harmlessly. -->
Arguments: `$ARGUMENTS`

The first word is TOOL, the second is ACTION, and the rest are ARGS. Every installed Agentic Fever Dream tool keeps its
action guide in `{{AFD_DIR}}`, one file per tool, named `<tool>.md`.

1. If TOOL is missing or `help`, or there is no `{{AFD_DIR}}/<TOOL>.md`:
   - List the guides in `{{AFD_DIR}}` (each file is one tool) with the one-line summary at the top of each.
   - Show the usage `/afd <tool> <action> [args]`.
   - Stop.
2. Otherwise read `{{AFD_DIR}}/<TOOL>.md` and carry out ACTION with ARGS exactly as that guide says. If ACTION is
   missing or unknown, show the actions the guide lists.
