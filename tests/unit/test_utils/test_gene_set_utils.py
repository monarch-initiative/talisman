"""Core tests."""

import logging
import shutil
import unittest
from pathlib import Path

from talisman.utils import gene_set_utils
from talisman.utils.gene_set_utils import (
    Gene,
    GeneSet,
    drop_genes_from_gene_set,
    fill_missing_gene_set_values,
    load_gene_sets,
    parse_gene_set,
)
from tests import GENE_SETS_DIR
from tests.unit import GENE_REQUESTS_CACHE_DB

logger = logging.getLogger(gene_set_utils.__name__)
logger.setLevel(level=logging.INFO)


class TestGeneSetUtils(unittest.TestCase):
    """Test ability to load and manipulate gene sets."""

    def setUp(self) -> None:
        """Set up."""
        cache_path = Path(gene_set_utils.GENE_REQUESTS_CACHE)
        if not cache_path.exists():
            print(f"Copying {GENE_REQUESTS_CACHE_DB} to {cache_path}")
            shutil.copy(str(Path(GENE_REQUESTS_CACHE_DB)), str(cache_path))

    def test_load_gene_sets(self):
        """Test loading from folder."""
        gene_sets = load_gene_sets(GENE_SETS_DIR, fill_missing=False)
        # print(yaml.dump(gene_sets.dict(), sort_keys=False))
        self.assertGreater(len(gene_sets.gene_sets), 3)

    def test_populate(self):
        """Test population of gene set."""
        gene_set = parse_gene_set(GENE_SETS_DIR / "Yamanaka-TFs.yaml", format="yaml")
        self.assertEqual(len(gene_set.gene_ids), 4)
        fill_missing_gene_set_values(gene_set)
        # print(yaml.dump(gene_set.dict(), sort_keys=False))
        self.assertEqual(len(gene_set.genes), 4)
        self.assertEqual(gene_set.taxon, "human")
        gene = gene_set.genes["HGNC:7553"]
        self.assertEqual(gene.symbol, "MYC")
        self.assertTrue(gene.ontological_synopsis.startswith("Enables several functions"))
        self.assertTrue(gene.narrative_synopsis.startswith("This gene is a proto-oncogene"))

    def test_drop_genes(self):
        """Test dropping a percentage of genes."""
        genes = {f"HGNC:{i}": Gene(id=f"HGNC:{i}", symbol=f"G{i}") for i in range(10)}
        gene_set = GeneSet(name="test", gene_ids=list(genes), genes=genes)
        dropped = drop_genes_from_gene_set(gene_set, 30)
        self.assertEqual(len(dropped.genes), 7)
        self.assertEqual(dropped.number_of_genes_dropped, 3)
        self.assertEqual(set(dropped.gene_ids), set(dropped.genes))
        self.assertLessEqual(set(dropped.genes), set(genes))
        self.assertEqual(len(gene_set.genes), 10, "the input gene set is unchanged")
