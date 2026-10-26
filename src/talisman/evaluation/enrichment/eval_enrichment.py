"""Eval enrichment engine with fixes for ontogpt 0.3.6."""

import logging
from dataclasses import dataclass
from typing import Any, Generator, Tuple

import tiktoken
from oaklib import get_implementation_from_shorthand
from oaklib.interfaces.class_enrichment_calculation_interface import (
    ClassEnrichmentCalculationInterface,
)
from oaklib.interfaces.obograph_interface import OboGraphInterface
from ontogpt.engines import create_engine
from ontogpt.evaluation.enrichment import eval_enrichment as ontogpt_eval_enrichment
from ontogpt.evaluation.enrichment.eval_enrichment import (
    CLOSURE,
    DATABASE_DIR,
    ENRICHMENT_MODELS,
    NARRATIVE_SYNOPSIS,
    NO_SYNOPSIS,
    ONTOLOGICAL_SYNOPSIS,
    RANDOM,
    RANK_BASED,
    STANDARD,
    STANDARD_NO_ONTOLOGY,
)
from ontogpt.evaluation.enrichment.eval_enrichment import EvalEnrichment as OntoGPTEvalEnrichment
from ontogpt.evaluation.enrichment.eval_enrichment import (
    GeneSetComparison,
    Overlap,
    get_symbol_to_gene_id_map,
)

from talisman.engines.enrichment import EnrichmentEngine
from talisman.utils.gene_set_utils import GeneSet, drop_genes_from_gene_set

# ontogpt 0.3.6's evaluator drops genes with its own copy of drop_genes_from_gene_set, which
# fails on Python 3.11+ (#25), and ontogpt is pinned. Give it talisman's fixed copy.
ontogpt_eval_enrichment.drop_genes_from_gene_set = drop_genes_from_gene_set

logger = logging.getLogger(__name__)

__all__ = [
    "CLOSURE",
    "DATABASE_DIR",
    "ENRICHMENT_MODELS",
    "EvalEnrichment",
    "GeneSetComparison",
    "NARRATIVE_SYNOPSIS",
    "NO_SYNOPSIS",
    "ONTOLOGICAL_SYNOPSIS",
    "Overlap",
    "RANDOM",
    "RANK_BASED",
    "STANDARD",
    "STANDARD_NO_ONTOLOGY",
    "get_symbol_to_gene_id_map",
]


def _model_name(model_entry: Any) -> str:
    """Resolve a model identifier from an ENRICHMENT_MODELS entry (string or dict)."""
    if isinstance(model_entry, dict):
        names = (
            model_entry.get("names")
            or model_entry.get("alternative_names")
            or [model_entry["name"]]
        )
        return str(names[0])
    return str(model_entry)


@dataclass
class EvalEnrichment(OntoGPTEvalEnrichment):
    """Evaluation engine for gene set enrichment (fixes ontogpt 0.3.6 bugs #49)."""

    def __post_init__(self) -> None:
        ontology = get_implementation_from_shorthand("sqlite:obo:go")
        if not isinstance(ontology, OboGraphInterface):
            raise TypeError
        self.ontology: ClassEnrichmentCalculationInterface = ontology
        self.engine: EnrichmentEngine = create_engine(None, EnrichmentEngine, model=self.model)
        for model_entry in ENRICHMENT_MODELS:
            model = _model_name(model_entry)
            self.engines[model] = create_engine(None, EnrichmentEngine, model=model)
            self.engines[model].add_resolver("sqlite:obo:hgnc")
        self.engine.add_resolver("sqlite:obo:hgnc")
        try:
            self.tokenizer_encoding = tiktoken.encoding_for_model(self.engine.model)
        except KeyError:
            self.tokenizer_encoding = tiktoken.get_encoding("cl100k_base")

    def compare_analysis(self, gene_set: GeneSet, **kwargs) -> GeneSetComparison:
        """Compare enrichment across LLM prompt variants and standard ontology methods."""
        payloads = {}
        logger.info(f"Gene symbols: {gene_set.gene_symbols}")
        models = [self.model] if self.model else [_model_name(m) for m in ENRICHMENT_MODELS]
        for model in models:
            engine = self.engines.get(model) or self.engine
            for method in [NO_SYNOPSIS, ONTOLOGICAL_SYNOPSIS, NARRATIVE_SYNOPSIS]:
                if method == ONTOLOGICAL_SYNOPSIS:
                    args = dict(ontological_synopsis=True)
                elif method == NARRATIVE_SYNOPSIS:
                    args = dict(ontological_synopsis=False)
                elif method == NO_SYNOPSIS:
                    args = dict(annotations=False)
                else:
                    raise AssertionError(f"Unknown method: {method}")
                for prompt_variant, end_marker in [("v1", "==="), ("v2", "###")]:
                    engine.end_marker = end_marker
                    payload = engine.summarize(gene_set, normalize=True, **args)  # type: ignore
                    payload.method = method
                    payload.prompt_variant = prompt_variant
                    model_method = f"{model}.{method}.{prompt_variant}"
                    payloads[model_method] = payload
        payloads[STANDARD] = self.standard_enrichment(gene_set)
        payloads[STANDARD_NO_ONTOLOGY] = self.standard_enrichment(gene_set, use_ontology=False)
        payloads[RANDOM] = self.random_enrichment(gene_set)
        payloads[RANK_BASED] = self.null_enrichment(gene_set)
        payloads[CLOSURE] = self.gene_term_closure(gene_set)
        for k in [STANDARD, STANDARD_NO_ONTOLOGY, RANDOM, RANK_BASED]:
            payloads[k].method = k
        return GeneSetComparison(
            name=gene_set.name,
            gene_symbols=gene_set.gene_symbols or [],
            gene_ids=gene_set.gene_ids,
            number_of_genes_swapped_out=gene_set.number_of_genes_dropped,
            payloads=payloads,
            **kwargs,
        )

    def get_annotation_tuples(self, path=None) -> Generator[Tuple[str, ...], None, None]:
        """Yield (symbol, term_id) pairs from a gzipped two-column TSV."""
        if path is None:
            path = DATABASE_DIR / "gene2go.tsv.gz"
        return super().get_annotation_tuples(path)

    def load_annotations(self, path=None, format=None) -> None:
        """Load annotations from a two-column TSV file or a GAF."""
        if path is None and not format:
            format = "tsv.gz"
        if path is not None:
            path = str(path)
        super().load_annotations(path=path, format=format)
