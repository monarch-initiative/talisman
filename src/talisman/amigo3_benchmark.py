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
from talisman.engines.amigo3_summarizer import parse_aspects, summarize_with_amigo3
from talisman.utils.gene_set_utils import GeneSet

logger = logging.getLogger(__name__)

SOURCES = ["claude", "go_top", "shown", "go_all", "mgsa_all"]


def expected_term_aspect(entry: Dict) -> str:
    """Infer the GO aspect ('P', 'F' or 'C') of a curated expected_go entry."""
    explicit = str(entry.get("aspect") or "").strip().upper()
    if explicit in ("P", "F", "C"):
        return explicit
    category = str(entry.get("category") or "").lower()
    if "component" in category:
        return "C"
    if "function" in category:
        return "F"
    return "P"


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

    tested_aspects = parse_aspects(
        summarize_options.get("aspect") or (summarize_options.get("parameters") or {}).get("aspect")
    )
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
        raw_expected = example.get("expected_go", [])
        expected = [e["id"] for e in raw_expected if expected_term_aspect(e) in tested_aspects]
        untested_aspect_expected = [
            e["id"] for e in raw_expected if expected_term_aspect(e) not in tested_aspects
        ]
        predictions = _predictions(summary)
        row = {
            "id": example["id"],
            "genes": len(mapped["genes"]),
            "unmapped": mapped.get("unmapped", 0),
            "expected": expected,
            "untested_aspect_expected": untested_aspect_expected,
            "scores": {s: score(expected, predictions[s], ancestors) for s in SOURCES},
            "ungrounded_ids": summary.ungrounded_ids,
            "nonsignificant_ids": summary.nonsignificant_ids,
            "misattributed_ids": summary.misattributed_ids,
            "cost_usd": summary.cost_usd,
            "cached": summary.cached,
            "summary": summary.summary,
        }
        rows.append(row)
    scored_rows = [r for r in rows if r["expected"]]
    means = {
        s: {
            "exact": (
                sum(r["scores"][s]["exact"] for r in scored_rows) / len(scored_rows)
                if scored_rows
                else 0.0
            ),
            "specific": (
                sum(r["scores"][s]["specific"] for r in scored_rows) / len(scored_rows)
                if scored_rows
                else 0.0
            ),
            "predicted": (
                sum(r["scores"][s]["predicted"] for r in rows) / len(rows) if rows else 0.0
            ),
        }
        for s in SOURCES
    }
    return {
        "species": species,
        "model": claude.model,
        "agent": bool(summarize_options.get("amigo3_dir")),
        "methods": methods,
        "aspects": tested_aspects,
        "sets": len(rows),
        "failed": failed,
        "mean": means,
        "total_cost_usd": sum(r["cost_usd"] or 0 for r in rows if not r["cached"]),
        "results": rows,
    }
