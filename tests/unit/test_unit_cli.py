"""CLI test."""

import gzip
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner
from oaklib.interfaces.obograph_interface import OboGraphInterface

from talisman.cli import main
from talisman.engines.enrichment import EnrichmentEngine, EnrichmentPayload
from talisman.evaluation.enrichment.eval_enrichment import (
    DATABASE_DIR,
    ENRICHMENT_MODELS,
    EvalEnrichment,
)
from talisman.utils.gene_set_utils import Gene, GeneSet
from tests import OUTPUT_DIR

CLI_OUTPUT_DIR = OUTPUT_DIR / "cli"


class TestCommandLineInterface(unittest.TestCase):
    """Tests all command-line subcommands."""

    def setUp(self) -> None:
        runner = CliRunner()
        self.runner = runner

    def test_main_help(self):
        result = self.runner.invoke(main, ["--help"])
        out = result.stdout
        self.assertIn("enrichment", out)
        self.assertEqual(0, result.exit_code)

    def test_eval_enrichment_drops_genes(self):
        """Test that eval-enrichment's evaluator can drop genes on Python 3.11+ (#25)."""
        # Skip EvalEnrichment.__init__, which loads models and resolvers.
        evaluator = EvalEnrichment.__new__(EvalEnrichment)
        sizes = []

        def compare_analysis(gene_set, **kwargs):
            sizes.append(len(gene_set.genes))
            return mock.Mock(dict=mock.Mock(return_value={}))

        evaluator.compare_analysis = compare_analysis
        genes = {f"HGNC:{i}": Gene(id=f"HGNC:{i}", symbol=f"G{i}") for i in range(10)}
        gene_set = GeneSet(name="test", gene_ids=list(genes), genes=genes)
        evaluator.evaluate_methods_on_gene_set(gene_set, n=3)
        self.assertEqual(sizes, [10, 9, 8])

    def test_eval_enrichment_post_init_and_compare_analysis(self):
        """Test that EvalEnrichment constructs and runs compare_analysis (#49)."""
        fake_ontology = mock.MagicMock(spec=OboGraphInterface)

        def fake_create_engine(_template, _cls, model=None):
            eng = mock.MagicMock()
            eng.model = model or "gpt-3.5-turbo"
            eng.summarize.side_effect = lambda gs, **kw: EnrichmentPayload(
                summary="s",
                term_strings=["peroxisome"],
                term_ids=["GO:0005777"],
                model=eng.model,
            )
            return eng

        with (
            mock.patch(
                "talisman.evaluation.enrichment.eval_enrichment.get_implementation_from_shorthand",
                return_value=fake_ontology,
            ),
            mock.patch(
                "talisman.evaluation.enrichment.eval_enrichment.create_engine",
                side_effect=fake_create_engine,
            ),
        ):
            evaluator = EvalEnrichment(model="gpt-4")

        self.assertEqual(set(evaluator.engines), set(ENRICHMENT_MODELS))
        for eng in evaluator.engines.values():
            eng.add_resolver.assert_called_once_with("sqlite:obo:hgnc")
        evaluator.engine.add_resolver.assert_called_once_with("sqlite:obo:hgnc")
        self.assertIsNotNone(evaluator.tokenizer_encoding)

        # Mock the non-LLM ontology baseline methods so compare_analysis runs without OAK DBs.
        baseline = EnrichmentPayload(term_strings=["t"], term_ids=["GO:0005777"])
        with (
            mock.patch.object(evaluator, "standard_enrichment", return_value=baseline),
            mock.patch.object(evaluator, "random_enrichment", return_value=baseline),
            mock.patch.object(evaluator, "null_enrichment", return_value=baseline),
            mock.patch.object(evaluator, "gene_term_closure", return_value=baseline),
        ):
            gs = GeneSet(name="pex", gene_symbols=["PEX1", "PEX2"], gene_ids=["HGNC:1", "HGNC:2"])
            comp = evaluator.compare_analysis(gs)

        self.assertEqual(comp.name, "pex")
        self.assertIn("gpt-4.ontological_synopsis.v1", comp.payloads)
        self.assertIn("gpt-4.narrative_synopsis.v2", comp.payloads)
        self.assertIn("standard", comp.payloads)

    def test_eval_enrichment_annotations_default_and_path_object(self):
        """Test get_annotation_tuples default path and load_annotations with Path (#49)."""
        evaluator = EvalEnrichment.__new__(EvalEnrichment)
        evaluator.loaded = False
        evaluator.ontology = mock.MagicMock()

        with tempfile.TemporaryDirectory() as tmp:
            tsv_gz = Path(tmp) / "gene2go.tsv.gz"
            with gzip.open(tsv_gz, "wt", encoding="utf-8") as fh:
                fh.write("PEX1\tGO:0005777\n")

            with (
                mock.patch(
                    "talisman.evaluation.enrichment.eval_enrichment.DATABASE_DIR",
                    Path(tmp),
                ),
                mock.patch(
                    "ontogpt.evaluation.enrichment.eval_enrichment.get_symbol_to_gene_id_map",
                    return_value={"PEX1": "HGNC:8850"},
                ),
            ):
                tuples = list(evaluator.get_annotation_tuples(None))
                self.assertEqual(tuples, [("PEX1", "GO:0005777")])
                evaluator.load_annotations(tsv_gz)

        self.assertTrue(evaluator.loaded)
        evaluator.ontology.add_associations.assert_called_once()
        self.assertTrue(str(DATABASE_DIR).endswith("database"))

    def test_create_gene_set_cli(self):
        """Test that create-gene-set CLI command runs with fixed EvalEnrichment (#49)."""
        fake_eval = mock.MagicMock()
        fake_eval.create_gene_set_from_term.return_value = GeneSet(
            name="term-GO:0005777",
            gene_symbols=["PEX1"],
            gene_ids=["HGNC:8850"],
        )
        with mock.patch("talisman.cli.EvalEnrichment", return_value=fake_eval):
            result = self.runner.invoke(
                main, ["create-gene-set", "-A", "tests/input/genes2go.tsv.gz", "GO:0005777"]
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        fake_eval.load_annotations.assert_called_once_with("tests/input/genes2go.tsv.gz")
        fake_eval.create_gene_set_from_term.assert_called_once_with("GO:0005777")
        self.assertIn("PEX1", result.stdout)

    def test_enrichment_cli_output_file_and_skip_annotator(self):
        """Test that enrichment -o writes to a file and forwards --skip-annotator."""
        fake_ke = mock.MagicMock(spec=EnrichmentEngine)
        fake_ke.client = mock.MagicMock()
        fake_ke.summarize.return_value = EnrichmentPayload(
            summary="peroxisome summary",
            term_strings=["peroxisome"],
            term_ids=["GO:0005777"],
            truncation_factor=1.0,
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("talisman.cli.create_engine", return_value=fake_ke),
        ):
            out_path = Path(tmp) / "eds.yaml"
            result = self.runner.invoke(
                main,
                [
                    "--skip-annotator",
                    "sqlite:obo:go",
                    "enrichment",
                    "-U",
                    "tests/input/genesets/EDS.yaml",
                    "-o",
                    str(out_path),
                ],
            )
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertEqual(fake_ke.skip_annotators, ["sqlite:obo:go"])
            self.assertIn("peroxisome summary", out_path.read_text(encoding="utf-8"))
