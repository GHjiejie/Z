---
name: text-stats
description: Calculate deterministic character, word, line, and frequent-word statistics for user-provided text. Use when the user asks to count or analyze text quantitatively; do not estimate counts manually.
---

# Text Stats

Call the `run_text_stats` tool with the exact text the user wants analyzed. Do not alter, translate, or normalize the input before passing it to the tool.

The tool executes [scripts/text_stats.py](scripts/text_stats.py), which contains the deterministic counting logic. Read the script only if the user asks how the counts are calculated or if the tool reports an error.

Present the tool result clearly and do not replace exact values with estimates.
