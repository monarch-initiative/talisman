#!/usr/bin/env bash
# PostToolUse hook (Write|Edit): format a Python file Claude edited under src/ or tests/ with the
# black and isort that `uv sync` installs from the dev group (the versions of the one-time reformat,
# #24), using the settings in pyproject.toml. Without them in .venv it does nothing, and it never
# blocks an edit.
input=$(cat)
if command -v jq >/dev/null; then
    file=$(jq -r '.tool_input.file_path // empty' <<<"$input")
else
    file=$(python3 -c 'import json, sys; print(json.load(sys.stdin)["tool_input"].get("file_path", ""))' <<<"$input")
fi
[ -f "$file" ] && [ ! -L "$file" ] || exit 0
# Compare physical paths, so `..` and symlinked directories can't reach files outside the repo.
root=$(cd "${CLAUDE_PROJECT_DIR:-$PWD}" && pwd -P) || exit 0
file="$(cd "$(dirname "$file")" && pwd -P)/$(basename "$file")" || exit 0
case "$file" in
    "$root"/src/*.py | "$root"/tests/*.py) ;;
    *) exit 0 ;;
esac
bin="$root/.venv/bin"
[ -x "$bin/black" ] && [ -x "$bin/isort" ] || exit 0
cd "$root" || exit 0
# black fails on a file that doesn't parse; isort would then mangle it, so stop there.
"$bin/black" --quiet "$file" && "$bin/isort" --quiet "$file"
exit 0
