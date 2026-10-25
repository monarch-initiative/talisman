"""CLI test."""

import unittest
from unittest import mock

from click.testing import CliRunner
from ontogpt.evaluation.enrichment.eval_enrichment import EvalEnrichment

from talisman.cli import main
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
