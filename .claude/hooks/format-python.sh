#!/usr/bin/env bash
# PostToolUse hook (Write|Edit): format a Python file Claude edited under src/ or tests/ with
# black and isort, the formatters `tox -e lint` runs, pinned to the versions of the one-time
# reformat (#24). Their settings come from pyproject.toml. Without uv it does nothing, and it
# never blocks the edit.
input=$(cat)
if command -v jq >/dev/null; then
    file=$(jq -r '.tool_input.file_path // empty' <<<"$input")
else
    file=$(python3 -c 'import json, sys; print(json.load(sys.stdin)["tool_input"].get("file_path", ""))' <<<"$input")
fi
root=${CLAUDE_PROJECT_DIR:-$PWD}
case "$file" in
    "$root"/src/*.py | "$root"/tests/*.py) ;;
    *) exit 0 ;;
esac
command -v uvx >/dev/null || exit 0
cd "$root" || exit 0
uvx --from black==26.10.1 black --quiet "$file"
uvx --from isort==9.0.2 isort --quiet "$file"
exit 0
