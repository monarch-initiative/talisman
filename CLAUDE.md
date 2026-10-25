# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment

- Use uv: `uv sync` builds `.venv` from `uv.lock`, dev tools included, and `uv run …` runs inside
  it. `.python-version` selects 3.11. The lock supports Python 3.9–3.11, and its lxml 5.0.0 has no
  macOS arm64 wheels for 3.9 or 3.10.
- Keep `ontogpt==0.3.6` and `setuptools<82`, and don't run `uv lock --upgrade`. talisman uses
  ontogpt 0.3.6 internals, and an unpinned resolve picks ontogpt 1.2 and openai 2.x, which break it.
- Code must run on Python 3.9, CI's oldest version, which only CI checks (the lock can't be
  installed on it on Apple Silicon). Use `Optional[...]`, `List[...]` and `Dict[...]`, never
  `X | None`, which pydantic evaluates at import. Don't use `match` statements.

## Tests and checks

- CI (`.github/workflows/qc.yml`, Python 3.9–3.11) runs `tox -e lint`, `tox -e flake8`,
  `tox -e mypy`, then `python -m unittest discover tests.unit`. `tox -e lint` rewrites files instead
  of checking them, so CI never fails on formatting (#28). `/ci-check` runs the same checks with
  formatting checked, so it is stricter than CI.
- Only `tests/unit` runs; `tox -e py` runs it with pytest. Bare `pytest` and `tests/integration`
  fail at import (they target ontogpt modules that don't exist here), and so does
  `import talisman.evaluation`.
- One test: `uv run python -m unittest tests.unit.test_unit_cli.TestCommandLineInterface.test_main_help`.
  `test_populate` calls the live Alliance API.
- A PostToolUse hook (`.claude/hooks/format-python.sh`) runs black and isort from `.venv` on each
  `.py` file edited under `src/` or `tests/`. They are pinned in the dev group (black 26.10.1, isort
  9.0.2), and their settings are in `pyproject.toml` (line length 100). The hook skips a file that
  doesn't parse.

## What is really ontogpt's

Most of talisman was copied from ontogpt 0.3.6, and much of it still calls ontogpt:

- `talisman enrichment` uses ontogpt's default prompt (`DEFAULT_ENRICHMENT_PROMPT`). Editing
  `src/talisman/prompts/enrichment/*.jinja2` changes nothing unless the file is passed with
  `--prompt-template`.
- `eval` runs ontogpt's evaluators, and the streamlit app runs ontogpt's engine. `eval-enrichment`
  and `create-gene-set` always crash, because ontogpt 0.3.6's `EvalEnrichment` fails while it is
  constructed (#49).
- The README's `ontogpt enrichment` example runs ontogpt's code, not talisman's.
- Most Makefile targets, the `src/ontogpt/templates` exclude in `tox.ini` and `.cruft.json` are
  ontogpt leftovers. Don't run `cruft update`.

## LLM calls, costs and caches

- A single run (one command, on one gene set, with the default model) is fine without asking. Ask
  first for anything larger: batches, benchmarks beyond `--limit 1`, `eval`, or a bigger model
  (`-m opus`, `-m gpt-4`). Run one set as a canary before a batch.
- `talisman enrichment` calls OpenAI through ontogpt's legacy client (openai 0.28; default
  `gpt-3.5-turbo`). It needs `OPENAI_API_KEY` or a key stored with `runoak set-apikey -e openai`,
  even when every answer is cached. Only `gpt-4` gets the 8k prompt budget; other models get 4k,
  and long gene descriptions are cut (`truncation_factor` in the output).
- `amigo3-summarize` and `amigo3-benchmark` call Claude through `claude -p`, billed to the signed-in
  Claude Code account, and need a running AmiGO 3 server (default `http://127.0.0.1:8030`).
  `--max-budget-usd` caps a call. See the README section and `/amigo3-run`.
- Answers are cached with no expiry in the working directory: `.openai_cache.db` and
  `.talisman_claude_cache.db` (`talisman --cache-db <file>` moves both), and
  `.gene_requests_cache.sqlite` (Alliance gene descriptions). A cached re-run is free, and it is not
  a fresh result. For a fresh answer, pass `--no-cache` to the amigo3 commands, or point
  `enrichment` at another `--cache-db` file.
- The first ontology lookup downloads several GB of `sqlite:obo:*` databases into `~/.data/oaklib/`.

## Git

- Stage files by path. Never `git add -A`, `git add .` or `git commit -a`: checkouts collect
  untracked LLM outputs, notebooks and scratch files that must not be committed.
- Work on a branch and open a PR against `main`; CI must pass.
