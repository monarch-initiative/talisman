# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment

- Use uv: `uv sync` builds `.venv` from `uv.lock`, and `uv run …` runs inside it. `.python-version`
  selects 3.11; the locked lxml 5.0.0 has no arm64 wheels for 3.9 or 3.10. Don't use a `venv/` if
  one exists: it is a non-editable pip install that doesn't see edits to `src/`.
- Keep `ontogpt==0.3.6` and don't run `uv lock --upgrade`. talisman uses ontogpt 0.3.6 internals,
  and an unpinned resolve picks ontogpt 1.2 and openai 2.x, which break it.

## Tests and checks

- CI (`.github/workflows/qc.yml`, Python 3.9–3.11) runs `tox -e lint`, `tox -e flake8`,
  `tox -e mypy`, then `python -m unittest discover tests.unit`. `/ci-check` runs the same checks
  locally.
- Only `tests/unit` runs. Bare `pytest`, `tox -e py` and `tests/integration` fail at import (they
  target ontogpt modules that don't exist here), and so does `import talisman.evaluation`.
- One test: `uv run python -m unittest tests.unit.test_unit_cli.TestCommandLineInterface.test_main_help`.
  `test_populate` calls the live Alliance API.
- `tox -e lint` rewrites files (black and isort without `--check`). A PostToolUse hook
  (`.claude/hooks/format-python.sh`) runs black 26.10.1 and isort 9.0.2 on each `.py` file edited
  under `src/` or `tests/`, with the settings in `pyproject.toml` (line length 100).

## What is really ontogpt's

Most of talisman was copied from ontogpt 0.3.6, and much of it still calls ontogpt:

- `talisman enrichment` uses ontogpt's default prompt (`DEFAULT_ENRICHMENT_PROMPT`). Editing
  `src/talisman/prompts/enrichment/*.jinja2` changes nothing unless the file is passed with
  `--prompt-template`.
- `eval` and `eval-enrichment` run ontogpt's evaluators, and the streamlit app runs ontogpt's
  engine. The README's `ontogpt enrichment` example runs ontogpt's code, not talisman's.
- Most Makefile targets, the `src/ontogpt/templates` exclude in `tox.ini` and `.cruft.json` are
  ontogpt leftovers. Don't run `cruft update`.

## LLM calls, costs and caches

- Single runs that call an LLM are fine. Batches and benchmarks need the user's yes first, and run
  one set as a canary before the rest.
- `talisman enrichment` calls OpenAI through ontogpt's legacy client (openai 0.28; default
  `gpt-3.5-turbo`). It needs `OPENAI_API_KEY` or a key stored with `runoak set-apikey -e openai`,
  even when every answer is cached. Only `gpt-4` gets the 8k prompt budget; other models get 4k,
  and long gene descriptions are cut (`truncation_factor` in the output).
- `amigo3-summarize` and `amigo3-benchmark` call Claude through `claude -p`, billed to the signed-in
  Claude Code account, and need a running AmiGO 3 server (default `http://127.0.0.1:8030`). See the
  README section and `/amigo3-run`.
- Answers are cached with no expiry in the working directory: `.openai_cache.db` (`talisman
  --cache-db` picks another file), `.talisman_claude_cache.db` (`--no-cache` skips it) and
  `.gene_requests_cache.sqlite` (Alliance gene descriptions). A cached re-run is free, and it is
  not a fresh result.
- The first ontology lookup downloads several GB of `sqlite:obo:*` databases into `~/.data/oaklib/`.

## Git

- Stage files by path. Never `git add -A`, `git add .` or `git commit -a`: working trees here
  collect untracked outputs, notebooks and scratch files that must not be committed.
- Work on a branch and open a PR against `main`; CI must pass.
