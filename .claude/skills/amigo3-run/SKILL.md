---
name: amigo3-run
description: Run talisman amigo3-summarize on a gene set, or amigo3-benchmark, against a local AmiGO 3 server - check or start the server, run one canary, check its output, and run batches only after the user agrees. Arguments - a gene set file or gene symbols, or "benchmark", plus any extra talisman options.
disable-model-invocation: true
---

Arguments: $ARGUMENTS

These commands make billed Claude Code calls (`claude -p` with the signed-in account;
`CLAUDE_CONFIG_DIR` picks the account). One gene set, or a benchmark with `--limit 1`, can run
without asking. Anything larger needs the user's yes first, with the number of sets and an
estimate: about $0.06 per set, or $0.09 with `--agent` (the 11-set benchmark in #4).

1. **Server.** `curl -sf http://127.0.0.1:8030/api/enrichment` (or the `--amigo3-url` in use)
   returns the build ID and species when the server is up. If it is down, ask where the
   amigo3-prototype checkout is (often `../amigo3-prototype`). Start the server there in the
   background with `uv run amigo3 serve --backend duckdb --genesets-url ''`, then poll the
   endpoint until it answers. Don't build a new AmiGO 3 database (`amigo3 fetch`, `amigo3 build`)
   without asking: it takes a long time and several GB.
2. **Canary.** Run one unit and write its output to a file:
   - a gene set: `uv run talisman amigo3-summarize -U <file> -o <name>.yaml`, or pass gene
     symbols as arguments instead of `-U`;
   - the benchmark: `uv run talisman amigo3-benchmark --limit 1 -o benchmark-1.yaml`.

   For database-checked summaries, add `--agent --amigo3-dir <amigo3-prototype checkout>`.
3. **Check the output, not the exit code.**
   - The YAML exists and has a summary.
   - `ungrounded_ids` is empty; if not, list the IDs.
   - Report `unresolved_genes`, `skipped_methods`, `cost_usd` and `num_turns`. With `--agent`,
     more than 2 turns means Claude used the database tools.
   - `cached: true` means the answer came from `.talisman_claude_cache.db`: it cost nothing and is
     not a fresh test. `--no-cache` forces a new call.
4. **Batch, only after the user agrees.** `amigo3-benchmark --limit 0` runs every example set and
   `--set-id` picks some. Report recall, ungrounded and non-significant citations, failed sets and
   the total cost.
