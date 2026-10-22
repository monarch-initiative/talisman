"""Command line interface for TALISMAN."""
import codecs
import logging
import pickle
import sys
from copy import copy
from dataclasses import dataclass
from io import BytesIO, TextIOWrapper
from typing import List, Optional, Union

import click
import yaml

from ontogpt import DEFAULT_MODEL, DEFAULT_MODEL_DETAILS, MODELS, __version__
from ontogpt.engines import create_engine
from ontogpt.engines.knowledge_engine import KnowledgeEngine
from ontogpt.evaluation.enrichment.eval_enrichment import EvalEnrichment
from ontogpt.evaluation.resolver import create_evaluator
from ontogpt.io.csv_wrapper import output_parser
from ontogpt.io.html_exporter import HTMLExporter
from ontogpt.io.markdown_exporter import MarkdownExporter
from ontogpt.io.owl_exporter import OWLExporter
from ontogpt.io.rdf_exporter import RDFExporter
from ontogpt.io.yaml_wrapper import dump_minimal_yaml
from ontogpt.templates.core import ExtractionResult

from talisman.amigo3_benchmark import run_benchmark
from talisman.amigo3_client import DEFAULT_AMIGO3_URL, DEFAULT_METHODS, METHODS, Amigo3Client
from talisman.claude_code import DEFAULT_CACHE_DB, DEFAULT_CLAUDE_MODEL, ClaudeCodeClient
from talisman.engines.amigo3_summarizer import summarize_with_amigo3
from talisman.engines.enrichment import EnrichmentEngine
from talisman.utils.gene_set_utils import (
    GeneSet,
    _is_human,
    fill_missing_gene_set_values,
    parse_gene_set,
)

__all__ = [
    "main",
]


@dataclass
class Settings:
    """Global command line settings."""

    cache_db: Optional[str] = None
    skip_annotators: Optional[List[str]] = None


settings = Settings()


def _as_text_writer(f):
    if isinstance(f, TextIOWrapper):
        return f
    else:
        return codecs.getwriter("utf-8")(f)


def write_extraction(
    results: ExtractionResult,
    output: BytesIO,
    output_format: str,
    knowledge_engine: KnowledgeEngine,
):
    """Write results of extraction to a given output stream."""
    # Check if this result contains anything writable first
    if results.extracted_object:
        exporter: Union[MarkdownExporter, HTMLExporter, RDFExporter, OWLExporter]

        if output_format not in ["pickle"]:
            output = _as_text_writer(output)

        if output_format == "pickle":
            output.write(pickle.dumps(results))
        elif output_format == "md":
            exporter = MarkdownExporter()
            exporter.export(results, output)
        elif output_format == "html":
            exporter = HTMLExporter(output=output)
            exporter.export(results, output)
        elif output_format == "yaml":
            output.write("---\n")  # type: ignore
            output.write(dump_minimal_yaml(results))  # type: ignore
        elif output_format == "turtle":
            exporter = RDFExporter()
            exporter.export(results, output, knowledge_engine.schemaview)
        elif output_format == "owl":
            exporter = OWLExporter()
            exporter.export(results, output, knowledge_engine.schemaview)
        elif output_format == "kgx":
            # output.write(write_obj_as_csv(results))
            output.write(dump_minimal_yaml(results))  # type: ignore
            with open("output.kgx.tsv") as secondoutput:
                for line in output_parser(obj=results, file=output):
                    secondoutput.write(line)
        else:
            output.write("---\n")  # type: ignore
            output.write(dump_minimal_yaml(results))  # type: ignore


def get_model_by_name(modelname: str):
    """Retrieve a model name and metadata from those available.

    Returns a dict describing the selected model.
    """
    found = False
    for knownmodel in MODELS:
        if modelname in knownmodel["alternative_names"] or modelname == knownmodel["name"]:
            selectmodel = knownmodel
            found = True
            logging.info(
                f"Found model: {selectmodel['name']}, provided by {selectmodel['provider']}."
            )
            if "not_implemented" in selectmodel or "deprecated" in selectmodel:
                logging.error(f"Model {selectmodel['name']} not implemented or is deprecated.")
                raise NotImplementedError
            break
    if not found:
        logging.warning(
            f"""Model name not recognized or not supported yet. Using default, {DEFAULT_MODEL}.
            See all models with `ontogpt list-models`"""
        )
        selectmodel = DEFAULT_MODEL_DETAILS

    return selectmodel


inputfile_option = click.option("-i", "--inputfile", help="Path to a file containing input text.")
template_option = click.option("-t", "--template", required=True, help="Template to use.")
target_class_option = click.option(
    "-T", "--target-class", help="Target class (if not already root)."
)
interactive_option = click.option(
    "--interactive/--no-interactive",
    default=False,
    show_default=True,
    help="Interactive mode - rather than call the LLM API it will prompt you do this.",
)
model_option = click.option(
    "-m",
    "--model",
    help="Model name to use, e.g. orca-mini-7b or gpt-4."
    " See all model names with ontogpt list-models.",
)
prompt_template_option = click.option(
    "--prompt-template", help="Path to a file containing the prompt."
)
recurse_option = click.option(
    "--recurse/--no-recurse", default=True, show_default=True, help="Recursively parse structures."
)
use_textract_options = click.option(
    "--use-textract/--no-use-textract",
    default=False,
    show_default=True,
    help="Use textract to extract text.",
)
output_option_wb = click.option(
    "-o", "--output", type=click.File(mode="wb"), default=sys.stdout, help="Output file."
)
output_option_txt = click.option(
    "-o", "--output", type=click.File(mode="w"), default=sys.stdout, help="Output file."
)
output_format_options = click.option(
    "-O",
    "--output-format",
    type=click.Choice(["json", "yaml", "pickle", "md", "html", "owl", "turtle", "jsonl"]),
    default="yaml",
    help="Output format.",
)
auto_prefix_option = click.option(
    "--auto-prefix",
    default="AUTO",
    help="Prefix to use for auto-generated classes. Default is AUTO.",
)
show_prompt_option = click.option(
    "--show-prompt/--no-show-prompt",
    default=False,
    show_default=True,
    help="If set, show all prompts passed to model through an API. Use with verbose setting.",
)


@click.group()
@click.option("-v", "--verbose", count=True)
@click.option("-q", "--quiet")
@click.option("--cache-db", help="Path to sqlite database to cache prompt-completion results")
@click.option(
    "--skip-annotator",
    multiple=True,
    help="Skip one or more annotators (e.g. --skip-annotator gilda)",
)
@click.version_option(__version__)
def main(verbose: int, quiet: bool, cache_db: str, skip_annotator):
    """CLI for TALISMAN.

    :param verbose: Verbosity while running.
    :param quiet: Boolean to be quiet or verbose.
    """
    logger = logging.getLogger()
    if verbose >= 2:
        logger.setLevel(level=logging.DEBUG)
    elif verbose == 1:
        logger.setLevel(level=logging.INFO)
    else:
        logger.setLevel(level=logging.WARNING)
    if quiet:
        logger.setLevel(level=logging.ERROR)
    logger.info(f"Logger {logger.name} set to level {logger.level}")
    if cache_db:
        settings.cache_db = cache_db
    if skip_annotator:
        settings.skip_annotators = list(skip_annotator)


@main.command()
@output_option_txt
@output_format_options
@click.option(
    "--annotation-path",
    "-A",
    required=True,
)
@click.argument("term")
def create_gene_set(term, output, output_format, annotation_path, **kwargs):
    """Create a gene set."""
    logging.info(f"Creating for {term}")
    evaluator = EvalEnrichment()
    evaluator.load_annotations(annotation_path)
    gene_set = evaluator.create_gene_set_from_term(term)
    print(yaml.dump(gene_set.dict(), sort_keys=False))


@main.command()
@output_option_txt
@output_format_options
@click.option("--fill/--no-fill", default=False)
@click.option(
    "--input-file",
    "-U",
    help="File with gene IDs to enrich (if not passed as arguments)",
)
def convert_geneset(input_file, output, output_format, fill, **kwargs):
    """Convert gene set to YAML."""
    gene_set = parse_gene_set(input_file)
    if fill:
        fill_missing_gene_set_values(gene_set)
    output.write(dump_minimal_yaml(gene_set.dict()))


@main.command()
@output_option_txt
@output_format_options
@model_option
@show_prompt_option
@click.option(
    "--resolver", "-r", help="OAK selector for the gene ID resolver. E.g. sqlite:obo:hgnc"
)
@click.option(
    "-C",
    "--context",
    help="domain e.g. anatomy, industry, health-related (NOT IMPLEMENTED - currently gene only)",
)
@click.option(
    "--strict/--no-strict",
    default=True,
    show_default=True,
    help="If set, there must be a unique mappings from labels to IDs",
)
@click.option(
    "--input-file",
    "-U",
    help="File with gene IDs to enrich (if not passed as arguments)",
)
@click.option(
    "--randomize-gene-descriptions-using-file",
    help="FOR EVALUATION ONLY. Swap out gene descriptions with genes from this gene set filefile",
)
@click.option(
    "--ontological-synopsis/--no-ontological-synopsis",
    default=True,
    show_default=True,
    help="If set, use automated rather than manual gene descriptions",
)
@click.option(
    "--combined-synopsis/--no-combined-synopsis",
    default=False,
    show_default=True,
    help="If set, both gene descriptions",
)
@click.option(
    "--end-marker",
    help="For testing minor variants of prompts",
)
@click.option(
    "--annotations/--no-annotations",
    default=True,
    show_default=True,
    help="If set, include annotations in the prompt",
)
@prompt_template_option
@interactive_option
@click.argument("genes", nargs=-1)
def enrichment(
    genes,
    context,
    input_file,
    resolver,
    output,
    model,
    show_prompt,
    interactive,
    end_marker,
    output_format,
    randomize_gene_descriptions_using_file,
    **kwargs,
):
    """Gene class summary enriching (TALISMAN).

    Algorithm:

    1. Map gene symbols to IDs using the resolver (unless IDs specified)
    2. Fetch gene descriptions using Alliance API
    3. Create a prompt using descriptions

    Limitations:

    It is very easy to exceed the max token length with GPT-3 models.

    Usage:

        ontogpt enrichment -r sqlite:obo:hgnc -U tests/input/genesets/dopamine.yaml

    Usage:

        ontogpt enrichment -r sqlite:obo:hgnc -U tests/input/genesets/dopamine.yaml
    """
    if model:
        selectmodel = get_model_by_name(model)
        model_source = selectmodel["provider"]

        if model_source != "OpenAI":
            raise NotImplementedError(
                "Model not yet supported for gene enrichment or enrichment evaluation."
            )

    if not genes and not input_file:
        raise ValueError("Either genes or input file must be passed")
    if genes:
        gene_set = GeneSet(name="TEMP", gene_symbols=genes)
    if input_file:
        if genes:
            raise ValueError("Either genes or input file must be passed")
        gene_set = parse_gene_set(input_file)
    if not gene_set:
        raise ValueError("No genes passed")
    ke = create_engine(None, EnrichmentEngine, model=model)
    if end_marker:
        ke.end_marker = end_marker
    if interactive:
        ke.client.interactive = True
    if settings.cache_db:
        ke.client.cache_db_path = settings.cache_db
    if not isinstance(ke, EnrichmentEngine):
        raise ValueError(f"Expected EnrichmentEngine, got {type(ke)}")
    if resolver:
        ke.add_resolver(resolver)
    if randomize_gene_descriptions_using_file:
        print("WARNING!! Randomly spiking gene descriptions")
        spike_gene_set = parse_gene_set(randomize_gene_descriptions_using_file)
        aliases = {}
        if not spike_gene_set.gene_symbols:
            raise ValueError("No gene symbols for spike set")
        syms = copy(gene_set.gene_symbols)
        if len(spike_gene_set.gene_symbols) < len(gene_set.gene_symbols):
            raise ValueError("Not enough genes in spike set")
        for sym in spike_gene_set.gene_symbols:
            if not syms:
                break
            aliases[sym] = syms.pop()
        results = ke.summarize(
            spike_gene_set, normalize=resolver is not None, gene_aliases=aliases, **kwargs
        )
    else:
        results = ke.summarize(gene_set, normalize=resolver is not None, **kwargs)
    if results.truncation_factor is not None and results.truncation_factor < 1.0:
        logging.warning(f"Text was truncated; factor = {results.truncation_factor}")
    output = _as_text_writer(output)
    if show_prompt:
        print(results.prompt)
    output.write(dump_minimal_yaml(results))


def amigo3_options(f):
    """Options shared by the AmiGO 3 commands."""
    options = [
        click.option(
            "--amigo3-url",
            default=DEFAULT_AMIGO3_URL,
            show_default=True,
            help="A running AmiGO 3 server (amigo3 serve --backend duckdb).",
        ),
        click.option(
            "--methods",
            default=",".join(DEFAULT_METHODS),
            show_default=True,
            help=f"Comma-separated enrichment methods: {', '.join(METHODS)}.",
        ),
        click.option(
            "--aspect",
            type=click.Choice(["P", "F", "C"]),
            default="P",
            show_default=True,
            help="GO aspect for the GO-term methods.",
        ),
        click.option(
            "--evidence",
            type=click.Choice(["all", "no_iea", "experimental", "iba"]),
            default="all",
            show_default=True,
            help="Annotation evidence for the GO-term methods.",
        ),
        click.option(
            "--max-terms",
            type=int,
            default=25,
            show_default=True,
            help="Most significant sets per method to show the model.",
        ),
        click.option(
            "-m",
            "--model",
            default=DEFAULT_CLAUDE_MODEL,
            show_default=True,
            help="Claude model alias (sonnet, opus, haiku) or full name.",
        ),
        click.option(
            "--agent/--no-agent",
            default=False,
            show_default=True,
            help="Let Claude query the AmiGO 3 build (read-only SQL) while it writes.",
        ),
        click.option(
            "--amigo3-dir",
            type=click.Path(exists=True, file_okay=False),
            help="amigo3-prototype checkout holding the server's build; needed for --agent.",
        ),
        click.option(
            "--max-budget-usd",
            type=float,
            help="Stop a Claude call that would spend more than this.",
        ),
        click.option(
            "--claude-command",
            default="claude",
            show_default=True,
            help="Claude Code executable. Set CLAUDE_CONFIG_DIR to choose its account.",
        ),
        click.option(
            "--cache/--no-cache",
            default=True,
            show_default=True,
            help="Reuse Claude replies from the cache (--cache-db, else " + DEFAULT_CACHE_DB + ").",
        ),
    ]
    for option in reversed(options):
        f = option(f)
    return f


def _amigo3_setup(
    methods, aspect, evidence, model, agent, amigo3_dir, max_budget_usd, claude_command, cache
):
    method_list = [m.strip() for m in methods.split(",") if m.strip()]
    unknown = [m for m in method_list if m not in METHODS]
    if unknown:
        raise click.BadParameter(f"Unknown methods: {', '.join(unknown)}", param_hint="--methods")
    if agent and not amigo3_dir:
        raise click.UsageError("--agent needs --amigo3-dir")
    claude = ClaudeCodeClient(
        model=model,
        command=[claude_command],
        cache_db=(settings.cache_db or DEFAULT_CACHE_DB) if cache else None,
        max_budget_usd=max_budget_usd,
    )
    options = {
        "parameters": {"aspect": aspect, "evidence": evidence},
        "amigo3_dir": amigo3_dir if agent else None,
    }
    return method_list, claude, options


@main.command("amigo3-summarize")
@output_option_txt
@amigo3_options
@click.option(
    "--input-file",
    "-U",
    help="Gene set YAML, as for the enrichment command (if genes are not passed as arguments)",
)
@click.option("--species", help="AmiGO 3 species key, e.g. human, pombe, worm (default: taxon)")
@click.option(
    "--gene-descriptions/--no-gene-descriptions",
    default=False,
    show_default=True,
    help="Also give the model the gene set's descriptions (fetched if missing).",
)
@show_prompt_option
@prompt_template_option
@click.argument("genes", nargs=-1)
def amigo3_summarize(
    genes,
    input_file,
    species,
    gene_descriptions,
    amigo3_url,
    methods,
    aspect,
    evidence,
    max_terms,
    model,
    agent,
    amigo3_dir,
    max_budget_usd,
    claude_command,
    cache,
    show_prompt,
    prompt_template,
    output,
):
    """Summarize a gene set with Claude, grounded in AmiGO 3 enrichment results.

    Runs the chosen AmiGO 3 enrichment methods, gives Claude their significant
    results, and checks every ID it cites against them (ungrounded_ids).

    Usage:

        talisman amigo3-summarize -U tests/input/genesets/HALLMARK_GLYCOLYSIS.yaml -o out.yaml
    """
    if bool(genes) == bool(input_file):
        raise click.UsageError("Pass either genes or --input-file")
    gene_set = (
        parse_gene_set(input_file) if input_file else GeneSet(name="TEMP", gene_symbols=genes)
    )
    if gene_descriptions:
        fill_missing_gene_set_values(gene_set)
    method_list, claude, options = _amigo3_setup(
        methods, aspect, evidence, model, agent, amigo3_dir, max_budget_usd, claude_command, cache
    )
    result = summarize_with_amigo3(
        gene_set,
        species or gene_set.taxon,
        Amigo3Client(amigo3_url),
        claude,
        method_list,
        max_terms=max_terms,
        gene_descriptions=gene_descriptions,
        prompt_template=prompt_template,
        **options,
    )
    if result.ungrounded_ids:
        logging.warning(f"Cited IDs not in the enrichment results: {result.ungrounded_ids}")
    if show_prompt:
        print(result.prompt)
    output.write(dump_minimal_yaml(result))


@main.command("amigo3-benchmark")
@output_option_txt
@amigo3_options
@click.option("--species", default="human", show_default=True, help="AmiGO 3 species key")
@click.option(
    "--limit",
    type=int,
    default=1,
    show_default=True,
    help="Number of example sets to run; 1 is a canary, 0 runs all.",
)
@click.option("--set-id", multiple=True, help="Only run these example set IDs")
def amigo3_benchmark(
    species,
    limit,
    set_id,
    amigo3_url,
    methods,
    aspect,
    evidence,
    max_terms,
    model,
    agent,
    amigo3_dir,
    max_budget_usd,
    claude_command,
    cache,
    output,
):
    """Score Claude summaries against AmiGO 3's example sets and their expected GO terms.

    Compares the GO IDs Claude cites with the significant hypergeometric and MGSA
    terms, as recall of the curated expected terms (exact, or allowing descendants).
    """
    method_list, claude, options = _amigo3_setup(
        methods, aspect, evidence, model, agent, amigo3_dir, max_budget_usd, claude_command, cache
    )
    report = run_benchmark(
        Amigo3Client(amigo3_url),
        claude,
        method_list,
        species=species,
        limit=limit or None,
        identifiers=list(set_id) or None,
        max_terms=max_terms,
        **options,
    )
    output.write(dump_minimal_yaml(report))


@main.command()
@output_option_txt
@click.option(
    "--strict/--no-strict",
    default=True,
    show_default=True,
    help="If set, there must be a unique mappings from labels to IDs",
)
@click.option(
    "--input-file",
    "-U",
    help="File with gene IDs to enrich (if not passed as arguments)",
)
@click.option(
    "--ontological-synopsis/--no-ontological-synopsis",
    default=True,
    show_default=True,
    help="If set, use automated rather than manual gene descriptions",
)
@click.option(
    "--combined-synopsis/--no-combined-synopsis",
    default=False,
    show_default=True,
    help="If set, both gene descriptions",
)
@click.option(
    "--annotations/--no-annotations",
    default=True,
    show_default=True,
    help="If set, include annotations in the prompt",
)
@click.option(
    "--number-to-drop",
    "-n",
    type=click.types.INT,
    default=1,
    help="Max number of genes to drop",
)
# @click.option(
#    "--randomize-gene-descriptions/--no-randomize-gene-descriptions",
#    help="DO NOT USE EXCEPT FOR EVALUATION PUPOSES."
# )
@click.option(
    "--annotations-path",
    "-A",
    help="Path to annotations",
)
@model_option
@click.argument("genes", nargs=-1)
def eval_enrichment(genes, input_file, number_to_drop, annotations_path, model, output, **kwargs):
    """Run enrichment using multiple methods."""
    if model:
        selectmodel = get_model_by_name(model)
        model_source = selectmodel["provider"]

        if model_source != "OpenAI":
            raise NotImplementedError(
                "Model not yet supported for gene enrichment or enrichment evaluation."
            )

    if not genes and not input_file:
        raise ValueError("Either genes or input file must be passed")
    if genes:
        gene_set = GeneSet(name="TEMP", gene_symbols=genes)
    if input_file:
        if genes:
            raise ValueError("Either genes or input file must be passed")
        gene_set = parse_gene_set(input_file)
    if not gene_set:
        raise ValueError("No genes passed")
    fill_missing_gene_set_values(gene_set)
    if not annotations_path:
        if not _is_human(gene_set):
            raise ValueError("No annotations path passed")
        annotations_path = "tests/input/genes2go.tsv.gz"
    eval_engine = EvalEnrichment(model=model)
    eval_engine.load_annotations(annotations_path)
    comps = eval_engine.evaluate_methods_on_gene_set(gene_set, n=number_to_drop, **kwargs)
    output.write(dump_minimal_yaml(comps))


@main.command()
@recurse_option
@model_option
@output_option_txt
@click.option(
    "--num-tests",
    type=click.INT,
    default=5,
    show_default=True,
    help="number of iterations to cycle through.",
)
@click.option(
    "--chunking/--no-chunking",
    default=False,
    show_default=True,
    help="If set, chunk input text, then prepare a separate prompt for each chunk."
    " Otherwise the full input text is passed.",
)
@click.argument("evaluator")
def eval(evaluator, num_tests, output, chunking, model, **kwargs):
    """Evaluate an extractor."""
    logging.info(f"Creating for {evaluator}")

    if model:
        selectmodel = get_model_by_name(model)
        modelname = selectmodel["alternative_names"][0]
    else:
        modelname = DEFAULT_MODEL

    evaluator = create_evaluator(
        name=evaluator, num_tests=num_tests, chunking=chunking, model=modelname
    )
    eos = evaluator.eval()
    output.write(dump_minimal_yaml(eos, minimize=False))


if __name__ == "__main__":
    main()
