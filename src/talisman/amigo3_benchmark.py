"""Score Claude summaries and plain enrichment against curated expected GO terms.

AmiGO 3 bundles example gene sets (MSigDB signatures) with the GO terms their
curated interpretations expect, from the monarch-initiative/genesets corpus. For
each set this runs summarize_with_amigo3 and measures how many expected terms
each source recovers:

- go: the significant hypergeometric GO terms shown to the model
- mgsa: the MGSA-active GO terms shown to the model
- claude: the GO IDs Claude cites in enriched_terms and themes

A hit is exact, or "specific": a cited term is the expected term or a descendant of
it over is_a/part_of. The expected lists are not exhaustive, so this is recall only.
"""

import logging
from typing import Dict, List, Optional

from oaklib import get_adapter
from oaklib.datamodels.vocabulary import IS_A, PART_OF

from talisman.amigo3_client import Amigo3Client
from talisman.claude_code import ClaudeCodeClient
from talisman.engines.amigo3_summarizer import summarize_with_amigo3
from talisman.utils.gene_set_utils import GeneSet

logger = logging.getLogger(__name__)

SOURCES = ["go", "mgsa", "claude"]


def score(expected: List[str], predicted: List[str], ancestors) -> Dict:
    """Recall of expected IDs among predicted IDs, exactly and allowing descendants."""
    exact = [e for e in expected if e in predicted]
    lineage = set()
    for p in predicted:
        lineage.update(ancestors(p))
    specific = [e for e in expected if e in lineage]
    n = len(expected) or 1
    return {
        "predicted": len(predicted),
        "exact": len(exact) / n,
        "specific": len(specific) / n,
        "missed": [e for e in expected if e not in lineage],
    }


def _predictions(summary) -> Dict[str, List[str]]:
    def significant(method):
        return [r["id"] for r in summary.enrichment.get(method, []) if r["significant"]]

    cited = summary.term_ids + [i for t in summary.themes for i in t.term_ids]
    return {
        "go": significant("go"),
        "mgsa": significant("mgsa"),
        "claude": list(dict.fromkeys(i for i in cited if i.startswith("GO:"))),
    }


def run_benchmark(
    amigo3: Amigo3Client,
    claude: ClaudeCodeClient,
    methods: List[str],
    species: str = "human",
    limit: Optional[int] = 1,
    identifiers: Optional[List[str]] = None,
    **summarize_options,
) -> Dict:
    """Summarize and score example gene sets; limit=1 runs a single canary set."""
    go = get_adapter("sqlite:obo:go")
    lineage_cache: Dict[str, set] = {}

    def ancestors(term: str) -> set:
        if term not in lineage_cache:
            found = go.ancestors(term, predicates=[IS_A, PART_OF], reflexive=True)
            lineage_cache[term] = set(found) | {term}
        return lineage_cache[term]

    examples = amigo3.example_genesets(species)
    if identifiers:
        examples = [e for e in examples if e["id"] in identifiers]
    if limit:
        examples = examples[:limit]
    rows = []
    for example in examples:
        logger.info(f"Benchmarking {example['id']}")
        mapped = amigo3.geneset(example["id"], species)
        gene_set = GeneSet(
            name=example["id"], description=example.get("description"), gene_symbols=mapped["genes"]
        )
        summary = summarize_with_amigo3(
            gene_set, species, amigo3, claude, methods, **summarize_options
        )
        expected = [e["id"] for e in example.get("expected_go", [])]
        predictions = _predictions(summary)
        rows.append(
            {
                "id": example["id"],
                "genes": len(mapped["genes"]),
                "unmapped": mapped.get("unmapped", 0),
                "expected": expected,
                "scores": {s: score(expected, predictions[s], ancestors) for s in SOURCES},
                "ungrounded_ids": summary.ungrounded_ids,
                "cost_usd": summary.cost_usd,
                "cached": summary.cached,
                "summary": summary.summary,
            }
        )
    means = {
        s: {
            k: sum(r["scores"][s][k] for r in rows) / len(rows) if rows else 0.0
            for k in ("exact", "specific", "predicted")
        }
        for s in SOURCES
    }
    return {
        "species": species,
        "model": claude.model,
        "agent": bool(summarize_options.get("amigo3_dir")),
        "methods": methods,
        "sets": len(rows),
        "mean": means,
        "total_cost_usd": sum(r["cost_usd"] or 0 for r in rows if not r["cached"]),
        "results": rows,
    }
