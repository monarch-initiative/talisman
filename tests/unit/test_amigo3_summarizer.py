"""Tests for AmiGO 3 enrichment summaries, with no server and no Claude calls."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from talisman.amigo3_benchmark import score
from talisman.amigo3_client import Amigo3Client, Amigo3Error
from talisman.claude_code import ClaudeCodeClient
from talisman.engines.amigo3_summarizer import select_rows, summarize_with_amigo3
from talisman.utils.gene_set_utils import Gene, GeneSet

STUDY = {
    "matched": [
        {"input": "HK1", "id": "UniProtKB:P19367", "symbol": "HK1", "match": "symbol"},
        {"input": "PKM", "id": "UniProtKB:P14618", "symbol": "PKM", "match": "symbol"},
        {"input": "ENO1", "id": "UniProtKB:P06733", "symbol": "ENO1", "match": "symbol"},
    ],
    "unresolved": ["NOTAGENE"],
    "ambiguous": [],
    "duplicates": [],
}


def go_term(term_id, label, fdr, posterior=None):
    genes = [{"id": m["id"], "symbol": m["symbol"]} for m in STUDY["matched"]]
    term = {
        "id": term_id,
        "label": label,
        "study_count": 3,
        "term_size": 20,
        "p_value": fdr / 10,
        "fdr": fdr,
        "genes": genes,
    }
    if posterior is not None:
        term["posterior"] = posterior
    return term


def response(method_label, terms):
    return {
        "build_id": "b1",
        "parameters": {
            "method_label": method_label,
            "target_label": "GO terms",
            "test": "one-sided hypergeometric",
            "sets": "reflexive is_a/part_of",
            "background": "annotated genes in species",
        },
        "counts": {"input": 4, "study": 3, "population": 18000, "tests": 9000},
        "study": STUDY,
        "terms": terms,
    }


RESULTS = {
    "go": response(
        "Over-representation (hypergeometric)",
        [
            go_term("GO:0006096", "glycolytic process", 0.001),
            go_term("GO:0005975", "carbohydrate metabolic process", 0.01),
            go_term("GO:0008152", "metabolic process", 0.3),
        ],
    ),
    "mgsa": response(
        "Model-based (MGSA, Bayesian)",
        [
            go_term("GO:0005975", "carbohydrate metabolic process", 0.01, posterior=0.1),
            go_term("GO:0006096", "glycolytic process", 0.001, posterior=0.9),
        ],
    ),
}


class FakeAmigo3(Amigo3Client):
    def options(self):
        return {"build_id": "b1"}

    def enrich(self, genes, species, method="go", **parameters):
        self.last_genes = genes
        if method not in RESULTS:
            raise Amigo3Error("This build has no GO-CAM models; rebuild with --repository")
        return RESULTS[method]


class FakeClaude(ClaudeCodeClient):
    def complete(self, prompt, system_prompt, json_schema):
        self.prompt, self.system_prompt = prompt, system_prompt
        return {
            "structured_output": {
                "summary": "Glycolysis.",
                "mechanism": "Glucose breakdown.",
                "themes": [
                    {
                        "name": "glycolysis",
                        "description": "Most genes are glycolytic enzymes.",
                        "term_ids": ["GO:0006096", "GO:9999999"],
                        "methods": ["go", "mgsa"],
                        "genes": ["HK1", "pkm", "MADEUP1"],
                    }
                ],
                "enriched_terms": [{"id": "GO:0006096", "label": "glycolytic process"}],
                "caveats": ["NOTAGENE was not found."],
            },
            "model": "claude-sonnet-5-5",
            "cost_usd": 0.01,
            "num_turns": 2,
            "cached": False,
        }


class TestSelectRows(unittest.TestCase):
    def test_fdr_threshold(self):
        table = select_rows("go", RESULTS["go"])
        self.assertEqual(2, table["n_significant"])
        self.assertEqual(["GO:0006096", "GO:0005975"], [r["id"] for r in table["rows"]])
        self.assertIn("HK1, PKM, ENO1", table["rows"][0]["genes"])

    def test_posterior_sorted_descending(self):
        table = select_rows("mgsa", RESULTS["mgsa"])
        self.assertEqual(["GO:0006096"], [r["id"] for r in table["rows"]])
        self.assertEqual("0.90", table["rows"][0]["stat"])

    def test_context_rows_when_nothing_significant(self):
        result = response("x", [go_term("GO:0008152", "metabolic process", 0.3)])
        table = select_rows("go", result)
        self.assertEqual(0, table["n_significant"])
        self.assertEqual(1, len(table["rows"]))
        self.assertFalse(table["rows"][0]["significant"])

    def test_gene_list_is_capped(self):
        table = select_rows("go", RESULTS["go"], max_genes=2)
        self.assertEqual("HK1, PKM (+1 more)", table["rows"][0]["genes"])


class TestSummarize(unittest.TestCase):
    def setUp(self):
        self.gene_set = GeneSet(name="glyco", gene_symbols=["HK1", "PKM", "ENO1", "NOTAGENE"])
        self.amigo3 = FakeAmigo3("http://amigo.test")
        self.claude = FakeClaude(cache_db=None)

    def summarize(self, **kwargs):
        return summarize_with_amigo3(
            self.gene_set, "human", self.amigo3, self.claude, ["go", "mgsa", "gocam"], **kwargs
        )

    def test_grounding(self):
        result = self.summarize()
        self.assertEqual(["GO:9999999"], result.ungrounded_ids)
        self.assertEqual(["MADEUP1"], result.unknown_genes)
        self.assertEqual(["GO:0006096"], result.term_ids)
        self.assertEqual("claude-sonnet-5-5", result.model)

    def test_skipped_method_is_recorded(self):
        result = self.summarize()
        self.assertEqual(["go", "mgsa"], result.methods)
        self.assertIn("gocam", result.skipped_methods)
        self.assertIn("gocam was not run", self.claude.prompt)

    def test_prompt_contents(self):
        result = self.summarize()
        self.assertIn("| GO:0006096 | glycolytic process | 3/20 | 0.001 |", result.prompt)
        self.assertNotIn("GO:0008152", result.prompt)
        self.assertIn("so not tested: NOTAGENE", result.prompt)
        self.assertEqual(["NOTAGENE"], result.unresolved_genes)
        self.assertNotIn("read-only SQL", self.claude.system_prompt)

    def test_agent_mode(self):
        with tempfile.TemporaryDirectory() as amigo3_dir:
            build = Path(amigo3_dir) / "data" / "builds" / "b1"
            build.mkdir(parents=True)
            (build / "integration.duckdb").touch()
            with mock.patch("talisman.engines.amigo3_summarizer.shutil.which", return_value="uv"):
                result = self.summarize(amigo3_dir=amigo3_dir)
        server = self.claude.mcp_config["mcpServers"]["amigo3"]
        self.assertTrue(result.agent)
        self.assertEqual(str(build.resolve()), server["args"][server["args"].index("--build") + 1])
        self.assertIn("mcp__amigo3__query_sql", self.claude.allowed_tools)
        self.assertIn("read-only SQL", self.claude.system_prompt)

    def test_agent_mode_needs_the_server_build(self):
        with tempfile.TemporaryDirectory() as amigo3_dir:
            with self.assertRaises(ValueError):
                self.summarize(amigo3_dir=amigo3_dir)

    def test_gene_descriptions(self):
        self.gene_set.genes = {
            "HK1": Gene(id="HK1", symbol="HK1", ontological_synopsis="Enables hexokinase.")
        }
        result = self.summarize(gene_descriptions=True)
        self.assertIn("- HK1: Enables hexokinase.", result.prompt)


class TestClaudeCodeClient(unittest.TestCase):
    REPLY = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 2,
        "total_cost_usd": 0.02,
        "modelUsage": {"claude-sonnet-5-5": {}},
        "result": '{"id": "GO:0006096"}',
        "structured_output": {"id": "GO:0006096"},
    }

    def run_client(self, client, returncode=0, stdout=None):
        completed = mock.Mock(
            returncode=returncode, stdout=json.dumps(self.REPLY) if stdout is None else stdout
        )
        completed.stderr = ""
        with mock.patch("talisman.claude_code.subprocess.run", return_value=completed) as run:
            reply = client.complete("prompt", "system", {"type": "object"})
        return reply, run

    def test_arguments_and_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ClaudeCodeClient(cache_db=str(Path(tmp) / "cache.db"))
            reply, run = self.run_client(client)
            args = run.call_args.args[0]
            self.assertEqual({"id": "GO:0006096"}, reply["structured_output"])
            self.assertEqual("claude-sonnet-5-5", reply["model"])
            self.assertFalse(reply["cached"])
            self.assertEqual("", args[args.index("--tools") + 1])
            self.assertIn("--strict-mcp-config", args)
            self.assertNotIn("--mcp-config", args)
            self.assertEqual("prompt", run.call_args.kwargs["input"])
            again, run = self.run_client(client)
            self.assertTrue(again["cached"])
            run.assert_not_called()

    def test_mcp_tools(self):
        client = ClaudeCodeClient(
            cache_db=None,
            mcp_config={"mcpServers": {"amigo3": {"command": "uv"}}},
            allowed_tools=["mcp__amigo3__query_sql"],
        )
        _, run = self.run_client(client)
        args = run.call_args.args[0]
        self.assertIn("mcp__amigo3__query_sql", args[args.index("--allowedTools") + 1])

    def test_error_result(self):
        client = ClaudeCodeClient(cache_db=None)
        error = {**self.REPLY, "is_error": True, "subtype": "error_max_budget_usd"}
        with self.assertRaises(Exception) as context:
            self.run_client(client, returncode=1, stdout=json.dumps(error))
        self.assertIn("error_max_budget_usd", str(context.exception))


class TestAmigo3Client(unittest.TestCase):
    def test_error_detail(self):
        session = mock.Mock()
        session.request.return_value = mock.Mock(
            status_code=422, json=lambda: {"detail": "Species 'mouse' is not loaded"}
        )
        client = Amigo3Client("http://amigo.test/", session=session)
        with self.assertRaises(Amigo3Error) as context:
            client.enrich(["HK1"], "mouse", "mgsa")
        self.assertIn("is not loaded", str(context.exception))
        url, body = session.request.call_args.args[1], session.request.call_args.kwargs["json"]
        self.assertEqual("http://amigo.test/api/enrichment", url)
        self.assertEqual("mgsa", body["method"])

    def test_unknown_method(self):
        with self.assertRaises(ValueError):
            Amigo3Client().enrich(["HK1"], "human", "kegg")


class TestBenchmarkScore(unittest.TestCase):
    def test_exact_and_specific(self):
        lineage = {"GO:child": {"GO:child", "GO:parent"}, "GO:other": {"GO:other"}}
        result = score(
            ["GO:parent", "GO:other", "GO:missed"], ["GO:child", "GO:other"], lineage.get
        )
        self.assertAlmostEqual(1 / 3, result["exact"])
        self.assertAlmostEqual(2 / 3, result["specific"])
        self.assertEqual(["GO:missed"], result["missed"])
