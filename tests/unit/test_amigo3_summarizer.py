"""Tests for AmiGO 3 enrichment summaries, with no server and no Claude calls."""

import json
import subprocess  # noqa: S404
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from talisman.amigo3_benchmark import run_benchmark, score
from talisman.amigo3_client import Amigo3Client, Amigo3Error
from talisman.claude_code import ClaudeCodeClient, ClaudeCodeError
from talisman.engines.amigo3_summarizer import (
    parse_aspects,
    select_rows,
    summarize_with_amigo3,
)
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
    "outside_background": ["UniProtKB:P06733"],
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
    results = RESULTS
    examples = [
        {"id": "SET_A", "description": "a", "expected_go": [{"id": "GO:0005975"}]},
        {"id": "SET_B", "description": "b", "expected_go": [{"id": "GO:0006096"}]},
    ]

    def options(self):
        return {"build_id": "b1"}

    def enrich(self, genes, species, method="go", **parameters):
        self.last_genes, self.last_parameters = genes, parameters
        if method not in self.results:
            raise Amigo3Error("This build has no GO-CAM models; rebuild with --repository")
        return self.results[method]

    def example_genesets(self, species):
        return self.examples

    def geneset(self, identifier, species):
        if identifier == "SET_B":
            raise Amigo3Error("GET /api/genesets/SET_B returned 404: not found")
        return {"genes": ["HK1", "PKM", "ENO1"], "unmapped": 0}


class FakeClaude(ClaudeCodeClient):
    last = None
    """The client that made the latest call (agent mode calls a configured copy)."""

    def complete(self, prompt, system_prompt, json_schema):
        self.prompt, self.system_prompt = prompt, system_prompt
        FakeClaude.last = self
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
                "checks": [{"question": "Evidence for HK1?", "finding": "IDA"}],
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

    def test_network_shows_connected_genes(self):
        term = {**go_term("GO:0090153", "x", 0.01), "study_count": 0, "overlap_genes": []}
        term["by_fdr"] = 0.01
        term["genes"] = [{"id": "UniProtKB:P55212", "symbol": "CASP6"}]
        table = select_rows("network", response("x", [term]))
        self.assertEqual("CASP6", table["rows"][0]["genes"])
        self.assertEqual("Study genes connected to the set", table["genes_label"])

    def test_truncated_count_is_a_floor(self):
        result = {**RESULTS["go"], "terms": RESULTS["go"]["terms"][:2], "truncated": True}
        self.assertTrue(select_rows("go", result)["at_least"])
        self.assertFalse(select_rows("go", RESULTS["go"])["at_least"])

    def test_gene_list_is_capped(self):
        table = select_rows("go", RESULTS["go"], max_genes=2)
        self.assertEqual("HK1, PKM (+1 more)", table["rows"][0]["genes"])

    def test_diversifies_beyond_max_terms_with_mgsa_and_gene_coverage(self):
        terms = [
            {
                "id": "GO:0000001",
                "label": "broad term 1",
                "study_count": 2,
                "term_size": 500,
                "p_value": 1e-6,
                "fdr": 1e-5,
                "genes": [
                    {"id": "UniProtKB:P19367", "symbol": "HK1"},
                    {"id": "UniProtKB:P14618", "symbol": "PKM"},
                ],
            },
            {
                "id": "GO:0000002",
                "label": "redundant broad term 2",
                "study_count": 2,
                "term_size": 400,
                "p_value": 2e-6,
                "fdr": 2e-5,
                "genes": [
                    {"id": "UniProtKB:P19367", "symbol": "HK1"},
                    {"id": "UniProtKB:P14618", "symbol": "PKM"},
                ],
            },
            {
                "id": "GO:0000003",
                "label": "redundant broad term 3",
                "study_count": 2,
                "term_size": 350,
                "p_value": 3e-6,
                "fdr": 3e-5,
                "genes": [
                    {"id": "UniProtKB:P19367", "symbol": "HK1"},
                    {"id": "UniProtKB:P14618", "symbol": "PKM"},
                ],
            },
            {
                "id": "GO:0000004",
                "label": "specific term covering ENO1",
                "study_count": 1,
                "term_size": 15,
                "p_value": 1e-3,
                "fdr": 0.01,
                "genes": [{"id": "UniProtKB:P06733", "symbol": "ENO1"}],
            },
            {
                "id": "GO:0000005",
                "label": "mgsa equivalent term",
                "study_count": 2,
                "term_size": 30,
                "p_value": 2e-3,
                "fdr": 0.02,
                "genes": [
                    {"id": "UniProtKB:P19367", "symbol": "HK1"},
                    {"id": "UniProtKB:P14618", "symbol": "PKM"},
                ],
            },
        ]
        table = select_rows(
            "go",
            response("Over-representation", terms),
            max_terms=3,
            priority_ids={"GO:0000005"},
        )
        self.assertEqual(5, table["n_significant"])
        self.assertEqual(
            ["GO:0000001", "GO:0000004", "GO:0000005"],
            [r["id"] for r in table["rows"]],
        )


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

    def test_all_results_requested(self):
        self.summarize()
        self.assertEqual(5000, self.amigo3.last_parameters["max_results"])

    def test_significant_ids_kept_beyond_shown_rows(self):
        result = self.summarize(max_terms=1)
        self.assertEqual(["GO:0006096"], [r["id"] for r in result.enrichment["go"]])
        self.assertEqual(["GO:0006096", "GO:0005975"], result.significant_ids["go"])

    def test_citing_a_context_row(self):
        self.amigo3.results = {"go": response("x", [go_term("GO:0006096", "glycolysis", 0.3)])}
        result = summarize_with_amigo3(self.gene_set, "human", self.amigo3, self.claude, ["go"])
        self.assertEqual(["GO:0006096"], result.nonsignificant_ids)
        self.assertEqual(["GO:9999999"], result.ungrounded_ids)
        self.assertEqual(["GO:0006096"], result.misattributed_ids)
        self.assertNotIn("GO:0006096", result.prompt)
        self.assertIn("- glycolysis (3/20, FDR (BH) 0.3: HK1, PKM, ENO1)", result.prompt)

    def test_misattributed_theme_ids(self):
        result = self.summarize()
        self.assertEqual([], result.misattributed_ids)
        self.amigo3.results = {
            "go": RESULTS["go"],
            "mgsa": response(
                "Model-based (MGSA, Bayesian)",
                [go_term("GO:0005975", "carbohydrate metabolic process", 0.01, posterior=0.9)],
            ),
        }
        result = self.summarize()
        self.assertEqual(["GO:0006096"], result.misattributed_ids)

    def test_multi_aspect_summarize(self):
        self.assertEqual(["P", "F", "C"], parse_aspects("all"))
        self.assertEqual(["P", "C"], parse_aspects("P,C"))
        with self.assertRaises(ValueError):
            parse_aspects("X")
        calls = []

        def enrich_multi(genes, species, method="go", **parameters):
            aspect = parameters.get("aspect")
            calls.append((method, aspect))
            if method == "gocam":
                return response("GO-CAM", [go_term("gomodel:123", "glycolysis model", 0.01)])
            if aspect == "C":
                return response(
                    method,
                    [go_term("GO:0005829", "cytosol", 0.002, posterior=0.8)],
                )
            return RESULTS[method]

        self.amigo3.enrich = enrich_multi
        result = summarize_with_amigo3(
            self.gene_set,
            "human",
            self.amigo3,
            self.claude,
            ["go", "mgsa", "gocam"],
            aspect="P,C",
        )
        self.assertEqual(
            [("go", "P"), ("go", "C"), ("mgsa", "P"), ("mgsa", "C"), ("gocam", "P")],
            calls,
        )
        self.assertEqual(
            ["GO:0006096", "GO:0005829", "GO:0005975"],
            result.significant_ids["go"],
        )

    def test_ambiguous_genes(self):
        study = {**STUDY, "ambiguous": [{"input": "TWIN", "candidates": []}]}
        self.amigo3.results = {"go": {**RESULTS["go"], "study": study}}
        result = summarize_with_amigo3(self.gene_set, "human", self.amigo3, self.claude, ["go"])
        self.assertEqual(["TWIN"], result.ambiguous_genes)
        self.assertEqual(["NOTAGENE"], result.unresolved_genes)
        self.assertIn("Matched several genes in AmiGO 3, so not tested: TWIN", result.prompt)

    def test_untested_genes_only_from_go_term_methods(self):
        self.amigo3.results = {"gocam": RESULTS["go"]}
        result = summarize_with_amigo3(self.gene_set, "human", self.amigo3, self.claude, ["gocam"])
        self.assertEqual([], result.untested_genes)

    def test_skipped_method_is_recorded(self):
        result = self.summarize()
        self.assertEqual(["go", "mgsa"], result.methods)
        self.assertIn("gocam", result.skipped_methods)
        self.assertIn("gocam was not run", FakeClaude.last.prompt)

    def test_prompt_contents(self):
        result = self.summarize()
        self.assertIn("| GO:0006096 | glycolytic process | 3/20 | 0.001 |", result.prompt)
        self.assertNotIn("GO:0008152", result.prompt)
        self.assertIn("so not tested: NOTAGENE", result.prompt)
        self.assertEqual(["NOTAGENE"], result.unresolved_genes)
        self.assertEqual(["ENO1"], result.untested_genes)
        self.assertIn("so not tested there: ENO1", result.prompt)
        self.assertNotIn("read-only SQL", FakeClaude.last.system_prompt)
        self.assertNotIn("- checks:", result.prompt)
        self.assertIsNone(FakeClaude.last.mcp_config)

    def test_agent_mode(self):
        with tempfile.TemporaryDirectory() as amigo3_dir:
            build = Path(amigo3_dir) / "data" / "builds" / "b1"
            build.mkdir(parents=True)
            (build / "integration.duckdb").touch()
            with mock.patch("talisman.engines.amigo3_summarizer.shutil.which", return_value="uv"):
                result = self.summarize(amigo3_dir=amigo3_dir)
        agent = FakeClaude.last
        server = agent.mcp_config["mcpServers"]["amigo3"]
        self.assertTrue(result.agent)
        self.assertEqual(str(build.resolve()), server["args"][server["args"].index("--build") + 1])
        self.assertEqual(
            ["mcp__amigo3__database_schema", "mcp__amigo3__query_sql"], agent.allowed_tools
        )
        self.assertIn("read-only SQL", agent.system_prompt)
        self.assertIn("- checks:", result.prompt)
        self.assertEqual("IDA", result.checks[0].finding)
        self.assertIsNone(self.claude.mcp_config, "the caller's client is not modified")

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
        error = {**self.REPLY, "is_error": True, "result": "API Error: Can't reach the API"}
        with self.assertRaises(ClaudeCodeError) as context:
            self.run_client(client, returncode=1, stdout=json.dumps(error))
        self.assertIn("Can't reach the API", str(context.exception))
        error = {**self.REPLY, "is_error": True, "result": "", "subtype": "error_max_budget_usd"}
        with self.assertRaises(ClaudeCodeError) as context:
            self.run_client(client, returncode=1, stdout=json.dumps(error))
        self.assertIn("error_max_budget_usd", str(context.exception))

    def test_timeout(self):
        client = ClaudeCodeClient(cache_db=None, timeout=1)
        expired = subprocess.TimeoutExpired("claude", 1)
        with mock.patch("talisman.claude_code.subprocess.run", side_effect=expired):
            with self.assertRaises(ClaudeCodeError):
                client.complete("prompt", "system", {"type": "object"})

    def test_agent_toolless_run_is_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_db = str(Path(tmp) / "cache.db")
            client = ClaudeCodeClient(
                cache_db=cache_db,
                mcp_config={"mcpServers": {"amigo3": {"command": "uv"}}},
                allowed_tools=["mcp__amigo3__query_sql"],
            )
            reply1, run1 = self.run_client(client)
            self.assertFalse(reply1["cached"])
            self.assertEqual(1, run1.call_count)
            # Tool-less agent reply (num_turns == 2) was not cached, so next call runs claude again
            tool_reply = {**self.REPLY, "num_turns": 4}
            reply2, run2 = self.run_client(client, stdout=json.dumps(tool_reply))
            self.assertFalse(reply2["cached"])
            self.assertEqual(1, run2.call_count)
            # Once tools were used (num_turns == 4), the reply is cached
            reply3, run3 = self.run_client(client)
            self.assertTrue(reply3["cached"])
            run3.assert_not_called()

    def test_resolved_model_and_alias_env_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_db = str(Path(tmp) / "cache.db")
            alias_client = ClaudeCodeClient(model="sonnet", cache_db=cache_db)
            reply, _ = self.run_client(alias_client)
            self.assertFalse(reply["cached"])
            # Pinned resolved model ID hits the cache entry written under the alias
            pinned_client = ClaudeCodeClient(model="claude-sonnet-5-5", cache_db=cache_db)
            pinned_reply, run = self.run_client(pinned_client)
            self.assertTrue(pinned_reply["cached"])
            run.assert_not_called()
            # Changing ANTHROPIC_DEFAULT_SONNET_MODEL changes the effective model key
            with mock.patch.dict(
                "os.environ", {"ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-6"}
            ):
                env_reply, env_run = self.run_client(alias_client)
                self.assertFalse(env_reply["cached"])
                self.assertEqual(1, env_run.call_count)


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


class TestBenchmark(unittest.TestCase):
    def run_sets(self, **kwargs):
        return run_benchmark(
            FakeAmigo3("http://amigo.test"),
            FakeClaude(cache_db=None),
            ["go", "mgsa"],
            ancestors=lambda term: {term},
            **kwargs,
        )

    def test_sources_and_failures(self):
        report = self.run_sets(limit=0)
        self.assertEqual(1, report["sets"])
        self.assertEqual("SET_B", report["failed"][0]["id"])
        scores = report["results"][0]["scores"]
        # Claude's ungrounded GO:9999999 is not scored; go_top matches Claude's list length
        self.assertEqual(1, scores["claude"]["predicted"])
        self.assertEqual(1, scores["go_top"]["predicted"])
        self.assertEqual(2, scores["go_all"]["predicted"])
        self.assertEqual(1.0, scores["go_all"]["exact"])
        self.assertEqual(0.0, scores["claude"]["exact"])

    def test_set_ids_ignore_limit(self):
        report = self.run_sets(identifiers=["SET_A", "SET_B"])
        self.assertEqual(1, report["sets"])
        self.assertEqual(1, len(report["failed"]))
        with self.assertRaises(ValueError):
            self.run_sets(identifiers=["NO_SUCH_SET"])

    def test_filters_expected_terms_by_tested_aspect(self):
        amigo3 = FakeAmigo3("http://amigo.test")
        amigo3.examples = [
            {
                "id": "SET_A",
                "description": "a",
                "expected_go": [
                    {"id": "GO:0006096", "category": "primary_process"},
                    {"id": "GO:0005829", "category": "core_component"},
                ],
            }
        ]
        report_p = run_benchmark(
            amigo3,
            FakeClaude(cache_db=None),
            ["go"],
            aspect="P",
            ancestors=lambda term: {term},
        )
        row_p = report_p["results"][0]
        self.assertEqual(["P"], report_p["aspects"])
        self.assertEqual(["GO:0006096"], row_p["expected"])
        self.assertEqual(["GO:0005829"], row_p["untested_aspect_expected"])
        self.assertEqual(1.0, row_p["scores"]["claude"]["exact"])

        report_pc = run_benchmark(
            amigo3,
            FakeClaude(cache_db=None),
            ["go"],
            aspect="P,C",
            ancestors=lambda term: {term},
        )
        row_pc = report_pc["results"][0]
        self.assertEqual(["P", "C"], report_pc["aspects"])
        self.assertEqual(2, len(row_pc["expected"]))
        self.assertEqual([], row_pc["untested_aspect_expected"])
