"""Score Claude summaries and plain enrichment against curated expected GO terms.

AmiGO 3 bundles example gene sets (MSigDB signatures) with the GO terms their
curated interpretations expect, from the monarch-initiative/genesets corpus. For
each set this runs summarize_with_amigo3 and measures how many expected terms
each source recovers:

- claude: GO IDs Claude cites (enriched_terms and themes) from significant table rows
- go_top: as many of the best hypergeometric GO terms as Claude cites
- shown: every significant GO term shown to Claude (go and mgsa tables)
- go_all: every significant hypergeometric GO term (often hundreds)
- mgsa_all: every MGSA-active GO term

go_top compares Claude with plain enrichment at the same list length; go_all is
the recall available to a reader of the full list.

A hit is exact, or "specific": a cited term is the expected term or a descendant of
it over is_a/part_of. The expected lists are not exhaustive, so this is recall only.
"""

import logging
from typing import Callable, Dict, List, Optional, Set

from oaklib import get_adapter
from oaklib.datamodels.vocabulary import IS_A, PART_OF

from talisman.amigo3_client import Amigo3Client, Amigo3Error
from talisman.claude_code import ClaudeCodeClient, ClaudeCodeError
from talisman.engines.amigo3_summarizer import summarize_with_amigo3
from talisman.utils.gene_set_utils import GeneSet

logger = logging.getLogger(__name__)

SOURCES = ["claude", "go_top", "shown", "go_all", "mgsa_all"]


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
    shown = [
        r["id"]
        for method in ("go", "mgsa")
        for r in summary.enrichment.get(method, [])
        if r["significant"]
    ]
    cited = summary.term_ids + [i for t in summary.themes for i in t.term_ids]
    claude = list(dict.fromkeys(i for i in cited if i.startswith("GO:") and i in shown))
    go_all = summary.significant_ids.get("go", [])
    return {
        "claude": claude,
        "go_top": go_all[: len(claude)],
        "shown": list(dict.fromkeys(shown)),
        "go_all": go_all,
        "mgsa_all": summary.significant_ids.get("mgsa", []),
    }


def go_ancestors() -> Callable[[str], Set[str]]:
    """Reflexive is_a/part_of ancestors from OAK's GO build (downloaded on first use)."""
    go = get_adapter("sqlite:obo:go")
    cache: Dict[str, Set[str]] = {}

    def ancestors(term: str) -> Set[str]:
        if term not in cache:
            found = go.ancestors(term, predicates=[IS_A, PART_OF], reflexive=True)
            cache[term] = set(found) | {term}
        return cache[term]

    return ancestors


def run_benchmark(
    amigo3: Amigo3Client,
    claude: ClaudeCodeClient,
    methods: List[str],
    species: str = "human",
    limit: Optional[int] = 1,
    identifiers: Optional[List[str]] = None,
    ancestors: Optional[Callable[[str], Set[str]]] = None,
    **summarize_options,
) -> Dict:
    """Summarize and score example gene sets; limit=1 runs a single canary set.

    A set that fails (server or Claude error) is reported under "failed" and the run
    goes on; means cover the completed sets.
    """
    if ancestors is None:
        ancestors = go_ancestors()

    examples = amigo3.example_genesets(species)
    if identifiers:
        unknown = sorted(set(identifiers) - {e["id"] for e in examples})
        if unknown:
            raise ValueError(f"No {species} example sets with IDs: {', '.join(unknown)}")
        examples = [e for e in examples if e["id"] in identifiers]
    elif limit:
        examples = examples[:limit]
    rows, failed = [], []
    for example in examples:
        logger.info(f"Benchmarking {example['id']}")
        try:
            mapped = amigo3.geneset(example["id"], species)
            gene_set = GeneSet(
                name=example["id"],
                description=example.get("description"),
                gene_symbols=mapped["genes"],
            )
            summary = summarize_with_amigo3(
                gene_set, species, amigo3, claude, methods, **summarize_options
            )
        except (Amigo3Error, ClaudeCodeError, ValueError) as e:
            logger.error(f"{example['id']} failed: {e}")
            failed.append({"id": example["id"], "error": str(e)})
            continue
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
                "nonsignificant_ids": summary.nonsignificant_ids,
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
        "failed": failed,
        "mean": means,
        "total_cost_usd": sum(r["cost_usd"] or 0 for r in rows if not r["cached"]),
        "results": rows,
    }
