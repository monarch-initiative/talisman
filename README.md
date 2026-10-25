# TALISMAN

## Introduction

_TALISMAN_ is a Python package for summarizing gene set functions using large language models (LLMs).

It uses the OntoGPT package to interface with LLMs.

[For more details, please see the full documentation.](https://monarch-initiative.github.io/talisman/)

## Quick Start

TALISMAN uses [uv](https://docs.astral.sh/uv/) to manage its environment:

```bash
git clone https://github.com/monarch-initiative/talisman.git
cd talisman
uv sync
uv run talisman --help
```

`uv sync` creates `.venv` with the versions locked in `uv.lock`, including the dev tools, and
installs talisman in editable mode. `.python-version` selects Python 3.11: on Apple Silicon the
locked lxml 5.0.0 has no wheels for Python 3.9 or 3.10. Run the unit tests with
`uv run python -m unittest discover tests.unit`.

## Functionality

The goal of gene summary enrichment is to assemble a textual summary of the functions of a set of genes and their products.

TALISMAN can run in three different ways:

1. Map gene symbols to IDs using the resolver (unless IDs are specified)
2. Fetch gene descriptions using Alliance API
3. Create a prompt using descriptions

Options:

* `-r`, `--resolver TEXT` - OAK selector for the gene ID resolver, e.g., `sqlite:obo:hgnc` for HGNC gene IDs.
* `-C`, `--context TEXT` - domain, e.g., anatomy, industry, health-related
* `--strict` / `--no-strict` - If set, there must be a unique mappings from labels to IDs. Defaults to True.
* `-U`, `--input-file TEXT` - Path to a file with gene IDs to enrich if not passed as arguments.
* `--randomize-gene-descriptions-using-file TEXT` - For evaluation only. Path to a file containing gene identifiers and descriptions; if this option is used, TALISMAN will swap out gene descriptions with those from this gene set file.
* `--ontological-synopsis` / `--no-ontological-synopsis` - If set, use automated rather than manual gene descriptions. Defaults to True.
* `--combined-synopsis` / `--no-combined-synopsis` - If set, combine gene descriptions. Defaults to False.
* `--end-marker TEXT` - Specify a character or string to end prompts with. For testing minor variants of prompts.
* `--annotations` / `--no-annotations` - If set, include annotations in the prompt. Defaults to True.
* `--prompt-template TEXT` - Path to a file containing the prompt.
* `--interactive` / `--no-interactive` - Interactive mode - rather than call the API, the function will present a walkthrough process. Defaults to False.

Example:

```bash
ontogpt enrichment -r sqlite:obo:hgnc -U tests/input/genesets/EDS.yaml
```

In this case, the prompt will include gene summaries retrieved from the database.

The response text will include, among other fields, a summary like this:

```text
Summary: The common function among these genes is their involvement in the regulation and organization of the extracellular matrix, particularly collagen fibril organization and biosynthesis.
```

### Summarize AmiGO 3 enrichment results with Claude

`talisman amigo3-summarize` grounds the summary in statistics. It runs a gene set through
the enrichment methods of [AmiGO 3](https://github.com/geneontology/amigo3-prototype):

| method | tests the genes against | significant when |
|---|---|---|
| `go` | GO terms, hypergeometric over-representation | FDR (BH) <= 0.05 |
| `mgsa` | GO terms, Bayesian model-based gene set analysis | posterior >= 0.5 |
| `gocam` | genes of each GO-CAM model | FDR (BH) <= 0.05 |
| `neighborhood` | connected neighborhoods of the GO-CAM gene network | FDR (BY) <= 0.05 |
| `network` | GO-term connectivity in the GO-CAM gene network | FDR (BY) <= 0.05 |

Claude then reads each method's significant sets and writes a summary, a mechanism and
themes that cite their IDs. Every cited ID is checked against those tables (`ungrounded_ids`)
and every cited gene against the input (`unknown_genes`). With `--agent`, Claude can also
query the AmiGO 3 database read-only while it writes, through the same tools as AmiGO 3's chat.

Requirements:

* A running AmiGO 3 server. Build one with `uv run amigo3 fetch` and
  `uv run amigo3 build --repository /path/to/ai-gene-review`, then
  `uv run amigo3 serve --backend duckdb` (no Solr needed). Without `--repository`, only
  `go` and `mgsa` work; the others are reported in `skipped_methods`.
* [Claude Code](https://claude.com/claude-code), signed in. TALISMAN runs `claude -p`, so calls
  are billed to that sign-in; set `CLAUDE_CONFIG_DIR` to choose between accounts. No API key
  is needed.

```bash
talisman amigo3-summarize -U tests/input/genesets/HALLMARK_GLYCOLYSIS.yaml -o glycolysis.yaml
talisman amigo3-summarize --agent --amigo3-dir ../amigo3-prototype -m opus HK1 PFKM PKM ENO1
```

Useful options: `--methods` (default `go,mgsa,gocam,neighborhood`), `--aspect P|F|C`,
`--evidence all|no_iea|experimental|iba`, `--species` (an AmiGO 3 species key; default the
gene set's taxon), `--gene-descriptions` (also give Claude the gene descriptions from the
`enrichment` command), `--max-budget-usd` and `--show-prompt`.

Output is YAML. Besides the summary, mechanism, themes and caveats it records the genes
AmiGO 3 could not use (`unresolved_genes`, `ambiguous_genes`, `untested_genes`), the rows
each method showed the model (`enrichment`), every significant set (`significant_ids`), and
two grounding checks: `ungrounded_ids` (cited, but in no table) and `nonsignificant_ids`
(cited, but only from a row listed as context).

Replies are cached in `.talisman_claude_cache.db` in the current directory (or the
`talisman --cache-db` path), keyed by model name, prompt and tools; pass `--no-cache` for a
fresh answer. An alias such as `sonnet` keeps its cached replies after it moves to a newer
model, so pass a full model name when that matters.

`talisman amigo3-benchmark` runs AmiGO 3's bundled example gene sets (11 human sets), each
with curated expected GO terms, and reports the recall of those terms by:

* `claude`: the GO IDs Claude cites from significant table rows
* `go_top`: the same number of top hypergeometric GO terms, for a like-for-like comparison
* `shown`: every significant GO term shown to Claude
* `go_all` and `mgsa_all`: every significant hypergeometric term (often hundreds) and every
  MGSA-active term

It runs one set by default; pass `--limit 0` for all of them, or `--set-id` to pick sets.

## Citation

The gene summarization approach used in TALISMAN is described further in: Joachimiak MP, Caufield JH, Harris NL, Kim H, Mungall CJ. Gene Set Summarization using Large Language Models. arXiv publication: <http://arxiv.org/abs/2305.13338>

## Acknowledgements

This project is part of the [Monarch Initiative](https://monarchinitiative.org/).
