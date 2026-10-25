---
name: ci-check
description: Run talisman's CI checks locally before pushing or opening a PR - black and isort in check mode, flake8, mypy and the unit tests. Use after changing code, or when asked whether CI will pass.
---

Run what `.github/workflows/qc.yml` runs, from the repo root, and report each step's result.

1. `uv sync --locked`. If it says the lock is out of date, `pyproject.toml` changed: run `uv lock`,
   check with `git diff uv.lock` that only the intended packages moved, and commit `uv.lock` with
   the change.
2. Formatting, in check mode. CI's `tox -e lint` rewrites files in place, so don't run it locally;
   run the formatters with the versions the format hook uses instead:
   - `uvx --from black==26.10.1 black --check src/ tests/`
   - `uvx --from isort==9.0.2 isort --check-only src/ tests/`

   To fix, run the same commands without `--check` / `--check-only` and review the diff.
3. `uv run tox -e flake8,mypy`. Their settings and plugins are in `tox.ini`; mypy's
   `--install-types` needs the network.
4. `uv run python -m unittest discover tests.unit`. `test_populate` calls the live Alliance API, so
   it needs the network. Don't use bare `pytest` or `tox -e py`: they collect `tests/integration`,
   which fails at import.

CI runs these on Python 3.9, 3.10 and 3.11; locally they run on `.python-version` (3.11). On a
failure, show the first errors with their file:line, and don't push until every step passes.
