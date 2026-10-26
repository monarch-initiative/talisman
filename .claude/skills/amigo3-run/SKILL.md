---
name: amigo3-run
description: Run talisman amigo3-summarize on a gene set, or amigo3-benchmark, against a local AmiGO 3 server - check or start the server, run one canary, check its output, and run batches only after the user agrees. Arguments - a gene set file or gene symbols, or "benchmark", plus any extra talisman options.
disable-model-invocation: true
---

Arguments: $ARGUMENTS

These commands make billed Claude Code calls (`claude -p` with the signed-in account;
`CLAUDE_CONFIG_DIR` picks the account).
- **Without asking:** one gene set, or a benchmark with `--limit 1`, with the default model
  (`sonnet`). That costs about $0.06 per set, or $0.09 with `--agent` (the 11-set benchmark in #4).
- **Ask first:** anything larger, or a bigger model (`-m opus`). Give the number of sets and an
  estimate.
- **Cap every call** with `--max-budget-usd` (0.50 is ample for one set with `sonnet`).
- **Outputs:** write them outside the repo, for example to a scratch directory or a path the user
  names, so they don't land as untracked files in the repo root.

1. **Server.** `curl -sf http://127.0.0.1:8030/api/enrichment` (or the `--amigo3-url` in use)
   returns the build ID and species when the server is up.
   - If it is down, ask where the amigo3-prototype checkout is (the README uses
     `../amigo3-prototype`). Start the server there in the background with
     `uv run amigo3 serve --backend duckdb --genesets-url ''`.
   - Poll the endpoint for up to two minutes. If it still doesn't answer, show the end of the
     server's log and stop: there may be no build.
   - Don't build a new AmiGO 3 database (`amigo3 fetch`, `amigo3 build`) without asking. It takes
     a long time and several GB.
2. **Canary.** Run one unit:
   - a gene set: `uv run talisman amigo3-summarize -U <file> --max-budget-usd 0.5 -o <dir>/<name>.yaml`,
     or pass gene symbols as arguments instead of `-U`;
   - the benchmark: `uv run talisman amigo3-benchmark --limit 1 --max-budget-usd 0.5 -o <dir>/benchmark-1.yaml`.

   For database-checked summaries, add `--agent --amigo3-dir <amigo3-prototype checkout>`.
3. **Check the output, not the exit code.**
   - **amigo3-summarize:**
     - The YAML has a summary.
     - `ungrounded_ids`, `nonsignificant_ids`, `misattributed_ids` and `unknown_genes` are empty; if not, list them.
     - Report `unresolved_genes`, `skipped_methods`, `cost_usd` and `num_turns`. With `--agent`,
       more than 2 turns means Claude used the database tools (replies with `num_turns <= 2` are not cached).
   - **amigo3-benchmark:**
     - `sets` is the number requested, and `failed` is empty. A failed set still exits 0, with its
       error under `failed`.
     - Report `total_cost_usd` and the `mean` scores.
   - **Either:** `cached: true` means the answer came from `.talisman_claude_cache.db`. It cost
     nothing and is not a fresh test; `--no-cache` forces a new call.
4. **Batch, only after the user agrees.** `amigo3-benchmark --limit 0` runs every example set, and
   `--set-id` picks some. Report recall, ungrounded and non-significant citations, failed sets and
   the total cost.
5. **Clean up.** If this skill started the server, stop it when the runs are done.
