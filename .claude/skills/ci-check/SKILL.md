---
name: ci-check
description: Run talisman's CI checks locally before pushing or opening a PR - formatting (checked), flake8, mypy, build metadata, and the unit tests. Use after changing code, or when asked whether CI will pass.
---

Run the checks from `.github/workflows/qc.yml` from the repo root, and report each step's result.

1. `uv sync --locked`. If it says the lock is out of date, `pyproject.toml` changed: run `uv lock`,
   check with `git diff uv.lock` that only the intended packages moved, and commit `uv.lock` with
   the change.
2. Formatting, in check mode, with the black and isort from the dev group (the versions the format
   hook and `tox -e lint` use):
   - `uv run --locked black --check --diff src/ tests/`
   - `uv run --locked isort --check-only --diff src/ tests/`

   If a file you changed fails, run the same command without `--check` / `--check-only` on that
   file and review the diff. If files you didn't touch fail, report them instead of reformatting
   them inside an unrelated change.
3. `uv run --locked tox -e lint,flake8,mypy`. Their settings and plugins are in `tox.ini`; mypy's
   `--install-types` needs the network.
4. `uv build && uvx twine check dist/*` to verify sdist/wheel metadata compatibility.
5. `uv run --locked python -m unittest discover tests.unit` (or `uv run --locked tox -e py`).
   `test_populate` calls the live Alliance API, so it needs the network. Don't run bare `pytest`:
   it collects `tests/integration`, which fails at import.

CI runs these on Python 3.9, 3.10 and 3.11; locally they run on `.python-version` (3.11), so
3.9-only problems show up only in CI. On a failure, show the first errors with their file:line.
