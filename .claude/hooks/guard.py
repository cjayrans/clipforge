#!/usr/bin/env python3
"""Claude Code PreToolUse guard for clipforge.

Reads the hook event JSON on stdin. Exit code 2 blocks the tool call and shows stderr to Claude.
Blocks: reading or printing secrets, destructive commands on data/ or git history, enabling live publishing,
and edits to .env files.
"""

from __future__ import annotations

import json
import re
import sys

BASH_RULES: list[tuple[str, str]] = [
    (r"(^|[\s;&|])(cat|less|more|head|tail|grep|rg|sed|awk|bat|source|\.)\s+[^|;&]*\.env(\s|$|\.)",
     "Reading .env is not allowed. Use clipforge.config to load settings."),
    (r"\b(printenv|env)\b\s*($|[|;&])", "Dumping the environment can expose secrets."),
    (r"\becho\s+[^|;&]*\$\{?(ANTHROPIC|GROQ|UPLOAD_POST|TELEGRAM|YOUTUBE)_", "Printing secrets is not allowed."),
    (r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*\s+[^|;&]*\bdata\b", "Deleting data/ is not allowed from Claude Code."),
    (r"\bgit\s+push\s+[^|;&]*(--force|-f)\b", "Force-push is not allowed."),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard is not allowed; ask the operator."),
    (r"PUBLISH_MODE\s*=\s*live", "Only the operator switches PUBLISH_MODE to live."),
]


def block(reason: str) -> None:
    print(f"Blocked by .claude/hooks/guard.py: {reason}", file=sys.stderr)
    sys.exit(2)


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input", {}) or {}

    if tool == "Bash":
        command = str(tool_input.get("command", ""))
        for pattern, reason in BASH_RULES:
            if re.search(pattern, command):
                block(reason)

    if tool in {"Edit", "Write", "MultiEdit"}:
        path = str(tool_input.get("file_path", ""))
        name = path.rsplit("/", 1)[-1]
        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            block("Editing .env files is not allowed.")
        content = str(tool_input.get("content", "")) + str(tool_input.get("new_string", ""))
        if re.search(r"PUBLISH_MODE\s*=\s*live", content) and not path.endswith((".md", "guard.py")):
            block("Only the operator switches PUBLISH_MODE to live.")

    sys.exit(0)


if __name__ == "__main__":
    main()
