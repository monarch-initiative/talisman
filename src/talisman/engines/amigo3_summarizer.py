"""Summarize a gene set with Claude, grounded in AmiGO 3 enrichment results.

Algorithm:

1. Run the chosen AmiGO 3 enrichment methods on the gene list.
2. Put each method's significant sets (or its top sets, marked as not significant)
   in a prompt, optionally with TALISMAN's gene descriptions.
3. Ask Claude for a summary, mechanism and themes that cite IDs from those tables.
   In agent mode Claude can also query the AmiGO 3 build read-only while it writes.
4. Check every cited ID against the tables and every cited gene against the input.
"""

import logging
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from jinja2 import Template
from pydantic import BaseModel

from talisman.amigo3_client import Amigo3Client, run_methods
from talisman.claude_code import ClaudeCodeClient
from talisman.prompts.amigo3 import DEFAULT_AMIGO3_PROMPT
from talisman.utils.gene_set_utils import GeneSet

logger = logging.getLogger(__name__)

# Per method: the statistic that decides significance, its threshold and its label.
STATISTICS = {
    "go": ("fdr", 0.05, "FDR (BH)"),
    "mgsa": ("posterior", 0.5, "MGSA posterior"),
    "gocam": ("fdr", 0.05, "FDR (BH)"),
    "neighborhood": ("by_fdr", 0.05, "FDR (BY)"),
    "network": ("by_fdr", 0.05, "FDR (BY)"),
}
HIGHER_IS_BETTER = {"posterior"}
CONTEXT_ROWS = 5
"""Rows shown for a method with no significant set."""

SYSTEM_PROMPT = (
    "You interpret gene set enrichment results for biologists. You are given a gene set and the "
    "results of statistical enrichment analyses that AmiGO 3 ran on it against the Gene Ontology "
    "(GO) and GO-CAM pathway models. Ground your interpretation in these results: cite their "
    "identifiers exactly as written and never invent identifiers. Where methods agree, say so; "
    "where they differ, say why that may be (for example, MGSA explains away ancestors and "
    "overlapping terms that the hypergeometric test lists separately). Prefer specific terms to "
    "general ones. You may use wider biological knowledge to connect the results, but say when "
    "a statement goes beyond them, and cite no identifier for it."
)
AGENT_PROMPT = (
    " You can also query the AmiGO 3 build that produced these results with read-only SQL "
    "tools: database_schema, query_sql and build_manifest. Inspect the schema before writing "
    "SQL. Use a few targeted queries (about ten at most) to check claims you are unsure of, "
    "such as which study genes carry a term and with what evidence, which genes and activities "
    "a GO-CAM model contains, or what an AI gene review says about a gene. Do not re-run the "
    "enrichment. Cite only identifiers from the result tables in themes and enriched_terms."
)
AMIGO3_TOOLS = ["database_schema", "query_sql", "build_manifest"]

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "mechanism": {"type": "string"},
        "themes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "term_ids": {"type": "array", "items": {"type": "string"}},
                    "methods": {"type": "array", "items": {"type": "string"}},
                    "genes": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "description", "term_ids", "methods", "genes"],
            },
        },
        "enriched_terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "label": {"type": "string"}},
                "required": ["id", "label"],
            },
        },
        "caveats": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "mechanism", "themes", "enriched_terms", "caveats"],
}


class Theme(BaseModel):
    name: str
    description: str
    term_ids: List[str] = []
    methods: List[str] = []
    genes: List[str] = []


class EnrichedTerm(BaseModel):
    id: str
    label: str = ""


class Amigo3Summary(BaseModel):
    """A Claude summary of a gene set and the AmiGO 3 results it rests on."""

    name: str
    species: str
    model: str = ""
    agent: bool = False
    amigo3_url: str = ""
    build_id: Optional[str] = None
    methods: List[str] = []
    skipped_methods: Dict[str, str] = {}
    input_genes: int = 0
    unresolved_genes: List[str] = []
    summary: str = ""
    mechanism: str = ""
    themes: List[Theme] = []
    enriched_terms: List[EnrichedTerm] = []
    term_ids: List[str] = []
    """IDs of enriched_terms, in order (the field TALISMAN's evaluations read)."""

    ungrounded_ids: List[str] = []
    """Cited IDs that were not in any result table given to the model."""

    unknown_genes: List[str] = []
    """Theme genes that are not in the input list."""

    caveats: List[str] = []
    enrichment: Dict[str, List[Dict]] = {}
    """The rows each method contributed to the prompt."""

    prompt: str = ""
    cost_usd: Optional[float] = None
    num_turns: Optional[int] = None
    cached: bool = False


def _stat_text(statistic: str, value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:.2f}" if statistic == "posterior" else f"{value:.2g}"


def _gene_list(genes: List[Dict], max_genes: int) -> str:
    symbols = [g.get("symbol") or g["id"] for g in genes]
    if len(symbols) > max_genes:
        return ", ".join(symbols[:max_genes]) + f" (+{len(symbols) - max_genes} more)"
    return ", ".join(symbols)


def _label(method: str, term: Dict) -> str:
    label = term.get("label") or term["id"]
    if method == "neighborhood":
        hubs = ", ".join(g["symbol"] for g in term.get("representatives", []))
        context = "; ".join(c["label"] for c in term.get("go_context", []))
        label += f" (hubs: {hubs}" + (f"; GO context: {context}" if context else "") + ")"
    elif term.get("status") and term["status"] != "production":
        label += f" ({term['status']})"
    return label.replace("|", "/")


def select_rows(method: str, result: Dict, max_terms: int = 25, max_genes: int = 20) -> Dict:
    """Pick the rows of one method's result that go into the prompt."""
    statistic, threshold, stat_label = STATISTICS[method]
    terms = [t for t in result.get("terms", []) if t.get(statistic) is not None]
    if statistic in HIGHER_IS_BETTER:
        terms.sort(key=lambda t: -t[statistic])
        significant = [t for t in terms if t[statistic] >= threshold]
        threshold_text = f"{stat_label} >= {threshold}"
    else:
        significant = [t for t in terms if t[statistic] <= threshold]
        threshold_text = f"{stat_label} <= {threshold}"
    shown = significant[:max_terms] if significant else terms[:CONTEXT_ROWS]
    rows = []
    for term in shown:
        genes = term.get("overlap_genes") or term.get("genes") or []
        rows.append(
            {
                "id": term["id"],
                "label": _label(method, term),
                "study_count": term.get("study_count"),
                "term_size": term.get("term_size"),
                "stat": _stat_text(statistic, term[statistic]),
                "genes": _gene_list(genes, max_genes),
                "significant": term in significant,
            }
        )
    parameters, counts = result.get("parameters", {}), result.get("counts", {})
    return {
        "key": method,
        "title": f"{parameters.get('method_label', method)}, {parameters.get('target_label', '')}",
        "test": parameters.get("test", ""),
        "sets": parameters.get("sets", ""),
        "background": parameters.get("background", ""),
        "study": counts.get("study"),
        "population": counts.get("population"),
        "tests": counts.get("tests"),
        "threshold": threshold_text,
        "stat_label": stat_label,
        "n_significant": len(significant),
        "rows": rows,
    }


def _study(results: Dict[str, Dict]) -> Dict:
    """Gene resolution as amigo3 reported it, preferring a GO-term method."""
    for method in ("go", "mgsa", "gocam", "neighborhood", "network"):
        if method in results:
            return results[method].get("study", {})
    return {}


def amigo3_mcp_config(amigo3_dir: str, build_id: str) -> Dict:
    """MCP config that serves a build's read-only DuckDB tools from an amigo3 checkout."""
    build = Path(amigo3_dir).resolve() / "data" / "builds" / build_id
    if not (build / "integration.duckdb").is_file():
        raise ValueError(f"No integration.duckdb for build {build_id} under {amigo3_dir}")
    uv = shutil.which("uv")
    if not uv:
        raise ValueError("--agent needs uv to start the amigo3 MCP server")
    args = ["run", "--project", str(Path(amigo3_dir).resolve()), "--extra", "chat"]
    args += ["python", "-m", "amigo3.chat_tools", "--build", str(build)]
    return {"mcpServers": {"amigo3": {"command": uv, "args": args}}}


def summarize_with_amigo3(
    gene_set: GeneSet,
    species: str,
    amigo3: Amigo3Client,
    claude: ClaudeCodeClient,
    methods: List[str],
    parameters: Optional[Dict] = None,
    max_terms: int = 25,
    max_genes: int = 20,
    gene_descriptions: bool = False,
    max_description_length: int = 300,
    amigo3_dir: Optional[str] = None,
    prompt_template: Optional[str] = None,
) -> Amigo3Summary:
    """Run AmiGO 3 enrichment on a gene set and have Claude summarize the results.

    :param amigo3_dir: an amigo3 checkout; if given, Claude can query the build (agent mode)
    """
    inputs = gene_set.gene_ids or gene_set.gene_symbols
    if not inputs:
        raise ValueError("Gene set has no gene IDs or symbols")
    build_id = amigo3.options().get("build_id")
    run = run_methods(amigo3, inputs, species, methods, parameters)
    results, skipped = run["results"], run["skipped"]
    if not results:
        raise ValueError(f"No enrichment method ran: {skipped}")
    study = _study(results)
    symbols = {m["input"]: m["symbol"] or m["id"] for m in study.get("matched", [])}
    unresolved = list(study.get("unresolved", []))
    unresolved += [a["input"] for a in study.get("ambiguous", [])]
    descriptions = {}
    if gene_descriptions and gene_set.genes:
        for gene_id, gene in gene_set.genes.items():
            text = gene.ontological_synopsis or gene.narrative_synopsis or ""
            if len(text) > max_description_length:
                text = text[:max_description_length].rsplit(" ", 1)[0] + "..."
            descriptions[gene_id] = descriptions[gene.symbol] = text
    genes = [{"symbol": symbols.get(i, i), "description": descriptions.get(i, "")} for i in inputs]
    tables = [select_rows(m, results[m], max_terms, max_genes) for m in methods if m in results]
    template_path = Path(prompt_template) if prompt_template else DEFAULT_AMIGO3_PROMPT
    prompt = Template(template_path.read_text()).render(
        name=gene_set.name,
        species=species,
        description=gene_set.description,
        genes=genes,
        with_descriptions=bool(descriptions),
        unresolved=unresolved,
        build_id=build_id,
        methods=tables,
        skipped=skipped,
    )
    system_prompt = SYSTEM_PROMPT
    if amigo3_dir:
        claude.mcp_config = amigo3_mcp_config(amigo3_dir, build_id)
        claude.allowed_tools = [f"mcp__amigo3__{tool}" for tool in AMIGO3_TOOLS]
        system_prompt += AGENT_PROMPT
    reply = claude.complete(prompt, system_prompt, SUMMARY_SCHEMA)
    out = reply["structured_output"]
    themes = [Theme(**t) for t in out.get("themes", [])]
    enriched = [EnrichedTerm(**t) for t in out.get("enriched_terms", [])]
    shown = {r["id"] for table in tables for r in table["rows"]}
    cited = [i for t in themes for i in t.term_ids] + [t.id for t in enriched]
    known_genes = {s.upper() for s in [*inputs, *symbols.values()]}
    theme_genes = [g for t in themes for g in t.genes]
    return Amigo3Summary(
        name=gene_set.name,
        species=species,
        model=reply["model"],
        agent=bool(amigo3_dir),
        amigo3_url=amigo3.url,
        build_id=build_id,
        methods=list(results),
        skipped_methods=skipped,
        input_genes=len(inputs),
        unresolved_genes=unresolved,
        summary=out.get("summary", ""),
        mechanism=out.get("mechanism", ""),
        themes=themes,
        enriched_terms=enriched,
        term_ids=[t.id for t in enriched],
        ungrounded_ids=list(dict.fromkeys(i for i in cited if i not in shown)),
        unknown_genes=list(dict.fromkeys(g for g in theme_genes if g.upper() not in known_genes)),
        caveats=out.get("caveats", []),
        enrichment={t["key"]: t["rows"] for t in tables},
        prompt=prompt,
        cost_usd=reply.get("cost_usd"),
        num_turns=reply.get("num_turns"),
        cached=reply.get("cached", False),
    )
