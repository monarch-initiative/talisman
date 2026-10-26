"""Unit tests for tabular GO enrichment, rank tests, and grounded Claude summarization."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner
from openpyxl import Workbook

from talisman.cli import main
from talisman.tabular_enrichment import (
    SHEET,
    bh,
    build_prompt,
    enrich,
    ground,
    read_annotations,
    run_tabular_enrichment,
    summarize_tabular_enrichment,
    term_tables,
    to_markdown,
)

HEADER = [
    "gene_id",
    "go_id",
    "go_ontology",
    "go_term",
    "Significant?",
    "pos/neg corr",
    "r (Pearson)",
    "FPKM average (samples)",
    "hypothetical protein?",
]


def _sample_rows():
    """Synthetic gene-to-GO rows with a positive cluster (GO:0006412 + parent GO:0044267)."""
    rows = []
    # 5 positively correlated significant genes in translation (GO:0006412) and parent (GO:0044267)
    for i in range(1, 6):
        gene = f"G_POS_{i}"
        r_val = round(0.80 + 0.02 * i, 4)
        for go_id, ont, name in (
            ("GO:0006412", "BP", "translation"),
            ("GO:0044267", "BP", "cellular protein metabolic process"),
            ("GO:0008150", "BP", "biological_process"),
        ):
            rows.append([gene, go_id, ont, name, "YES", "pos", r_val, 100.0 + i, "no"])
    # 1 extra non-significant gene in parent GO:0044267 so GO:0006412 has smaller background_count
    rows.append(
        [
            "G_BG_EXTRA",
            "GO:0044267",
            "BP",
            "cellular protein metabolic process",
            "NO",
            "pos",
            0.10,
            20.0,
            "no",
        ]
    )
    # 15 background negatively correlated genes in transport (GO:0006810)
    for i in range(1, 16):
        gene = f"G_NEG_{i}"
        sig = "YES" if i <= 4 else "NO"
        r_val = round(-0.75 - 0.01 * i, 4) if i <= 4 else round(-0.10 - 0.01 * i, 4)
        hyp = "yes" if i == 15 else "no"
        rows.append([gene, "GO:0006810", "BP", "transport", sig, "neg", r_val, 50.0, hyp])
    return rows


def _write_xlsx(path: Path, rows=None, sheet: str = SHEET, header=None) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(header if header is not None else HEADER)
    for r in rows if rows is not None else _sample_rows():
        ws.append(r)
    wb.save(path)


class TestTabularEnrichment(unittest.TestCase):
    """Tests for talisman.tabular_enrichment and its CLI subcommands."""

    def test_bh_monotonic_and_empty(self):
        self.assertEqual(bh([]), [])
        pvals = [0.001, 0.04, 0.03, 0.5]
        adj = bh(pvals)
        self.assertEqual(len(adj), 4)
        self.assertAlmostEqual(adj[0], 0.004)
        # 0.03 * 4 / 2 = 0.06, capped by rank 3 (0.04 * 4 / 3 = 0.05333...)
        self.assertAlmostEqual(adj[2], 0.04 * 4 / 3)
        self.assertAlmostEqual(adj[1], 0.04 * 4 / 3)
        self.assertTrue(all(0.0 <= q <= 1.0 for q in adj))

    def test_xlsx_and_tsv_enrichment_and_collapsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            xlsx_path = tmp_path / "input.xlsx"
            tsv_path = tmp_path / "input.tsv"
            _write_xlsx(xlsx_path)
            tsv_lines = ["\t".join(HEADER), ""] + [
                "\t".join(str(x) for x in r) for r in _sample_rows()
            ]
            tsv_path.write_text("\n".join(tsv_lines) + "\n\n", encoding="utf-8")

            genes_x, terms_x, tg_x = read_annotations(xlsx_path)
            genes_t, terms_t, tg_t = read_annotations(tsv_path)
            self.assertEqual(genes_x, genes_t)
            self.assertEqual(terms_x, terms_t)
            self.assertEqual(tg_x, tg_t)

            outdir = tmp_path / "results"
            summary = run_tabular_enrichment(xlsx_path, outdir)
            self.assertEqual(summary["background_genes"], 21)
            self.assertEqual(summary["annotated_hypothetical_genes"], 1)
            # Root GO:0008150 excluded; 3 terms tested (GO:0006412, GO:0044267, GO:0006810)
            self.assertEqual(summary["directions"]["pos"]["tested_terms"]["BP"], 3)
            # Both GO:0006412 (bg=5) and GO:0044267 (bg=6) share the 5 study genes;
            # GO:0006412 is representative (smaller background_count).
            self.assertEqual(summary["directions"]["pos"]["significant_terms"]["BP"], 2)
            self.assertEqual(summary["directions"]["pos"]["representative_terms"]["BP"], 1)

            sig, sug = term_tables(outdir, "up")
            by_id = {t["go_id"]: t for t in sig}
            # GO:0006412 and GO:0044267 share the 5 study genes; only GO:0006412 has "ora"
            self.assertIn("ora", by_id["GO:0006412"])
            self.assertNotIn("ora", by_id["GO:0044267"])
            self.assertEqual(sug, [])

            prompt = build_prompt("up", sig, sug, context="Custom context.", condition="salinity")
            self.assertIn("Custom context.", prompt)
            self.assertIn("with salinity", prompt)
            self.assertIn("GO:0006412", prompt)

            sug_sample = [{"go_id": "GO:0006810", "go_ontology": "BP", "go_term": "transport"}]
            reply = {
                "summary": "Translation rises (GO:0006412).",
                "themes": [
                    {
                        "theme": "Translation induction",
                        "go_ids": ["GO:0006412 (translation)"],
                        "evidence": "5/5 genes, FDR < 0.01; see also GO:0006810",
                        "interpretation": "Ribosomal synthesis increases.",
                    }
                ],
                "hypotheses": [
                    {
                        "hypothesis": "Possible ungrounded term",
                        "go_ids": ["GO:9999999"],
                        "why_tentative": "Synthetic check",
                    }
                ],
                "caveats": ["Synthetic dataset"],
            }
            g = ground(reply, sig, sug_sample)
            self.assertEqual(g["cited_ids"], ["GO:0006412", "GO:0006810", "GO:9999999"])
            self.assertEqual(g["ungrounded_ids"], ["GO:9999999"])
            self.assertEqual(g["theme_ids_not_significant"], ["GO:0006810"])

            md = to_markdown(
                "up",
                {
                    "direction": "up",
                    "model": "claude-sonnet-4-6",
                    "cost_usd": 0.01,
                    "cached": False,
                    "significant_terms": len(sig),
                    "suggestive_terms": 1,
                    "grounding": g,
                    "reply": reply,
                },
                condition="salinity",
            )
            self.assertIn("with salinity", md)

    def test_validation_and_edge_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            empty_tsv = tmp_path / "empty.tsv"
            empty_tsv.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Empty annotation table"):
                read_annotations(empty_tsv)

            bad_col_tsv = tmp_path / "bad_col.tsv"
            bad_col_tsv.write_text("gene_id\tgo_id\nG1\tGO:0006412\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Missing required column"):
                read_annotations(bad_col_tsv)

            bad_ont_tsv = tmp_path / "bad_ont.tsv"
            bad_ont_tsv.write_text(
                "\t".join(HEADER)
                + "\n12345\tGO:0006412\tUNKNOWN\ttranslation\tYES\tpos\t0.8\t10.0\tno\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Unknown GO ontology"):
                read_annotations(bad_ont_tsv)

            # Numeric gene_id and single-letter ontology alias ("P" -> "BP")
            alias_xlsx = tmp_path / "alias.xlsx"
            _write_xlsx(
                alias_xlsx,
                rows=[[101, " GO:0006412 ", "P", "translation", "YES", "pos", 0.85, 50.0, "no"]],
            )
            genes, terms, tg = read_annotations(alias_xlsx)
            self.assertIn("101", genes)
            self.assertEqual(terms["GO:0006412"], ("BP", "translation"))
            self.assertEqual(tg["GO:0006412"], {"101"})

            with self.assertRaisesRegex(ValueError, "Worksheet 'missing' not found"):
                read_annotations(alias_xlsx, sheet="missing")

            with self.assertRaisesRegex(ValueError, "direction must be"):
                enrich(genes, terms, tg, "invalid")
            with self.assertRaisesRegex(ValueError, "direction must be"):
                term_tables(tmp_path, "invalid")
            with self.assertRaisesRegex(ValueError, "direction must be"):
                build_prompt("invalid", [], [])
            with self.assertRaisesRegex(ValueError, "direction must be"):
                summarize_tabular_enrichment(tmp_path, "invalid", mock.MagicMock())

    def test_cli_tabular_enrichment_and_summarize(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            xlsx_path = tmp_path / "input.xlsx"
            outdir = tmp_path / "results"
            _write_xlsx(xlsx_path)

            res1 = runner.invoke(main, ["tabular-enrichment", str(xlsx_path), str(outdir)])
            self.assertEqual(res1.exit_code, 0, msg=res1.output)
            self.assertTrue((outdir / "enrichment_pos.tsv").exists())
            self.assertTrue((outdir / "enrichment_neg.tsv").exists())
            self.assertTrue((outdir / "rank_test.tsv").exists())
            self.assertTrue((outdir / "enrichment_summary.json").exists())

            ctx_file = tmp_path / "ctx.txt"
            ctx_file.write_text("File context", encoding="utf-8")
            res_both_ctx = runner.invoke(
                main,
                [
                    "tabular-summarize",
                    str(outdir),
                    "up",
                    "--context",
                    "Inline",
                    "--context-file",
                    str(ctx_file),
                ],
            )
            self.assertNotEqual(res_both_ctx.exit_code, 0)
            self.assertIn("either --context or --context-file", res_both_ctx.output)

            res_dry = runner.invoke(
                main,
                [
                    "tabular-summarize",
                    str(outdir),
                    "up",
                    "--context",
                    "Test organism",
                    "--condition",
                    "salinity",
                    "--dry-run",
                ],
            )
            self.assertEqual(res_dry.exit_code, 0, msg=res_dry.output)
            self.assertIn("Test organism", res_dry.output)
            self.assertIn("with salinity", res_dry.output)
            self.assertIn("GO:0006412", res_dry.output)

            fake_reply = {
                "model": "claude-sonnet-4-6",
                "cost_usd": 0.05,
                "cached": False,
                "structured_output": {
                    "summary": "Translation genes change (GO:0006412).",
                    "themes": [
                        {
                            "theme": "Translation",
                            "go_ids": ["GO:0006412"],
                            "evidence": "ORA + rank test",
                            "interpretation": "Core biosynthetic response.",
                        }
                    ],
                    "hypotheses": [],
                    "caveats": ["Test caveat."],
                },
            }
            with mock.patch("talisman.cli.ClaudeCodeClient") as mock_cls:
                mock_cls.return_value.complete.return_value = fake_reply
                res_up = runner.invoke(
                    main,
                    ["tabular-summarize", str(outdir), "up", "--context-file", str(ctx_file)],
                )
                self.assertEqual(res_up.exit_code, 0, msg=res_up.output)
                parsed_up = json.loads(res_up.output)
                self.assertEqual(parsed_up["direction"], "up")
                self.assertEqual(parsed_up["grounding"]["ungrounded_ids"], [])

                res_both = runner.invoke(
                    main,
                    ["tabular-summarize", str(outdir), "both", "--context", "Test organism"],
                )
                self.assertEqual(res_both.exit_code, 0, msg=res_both.output)
                parsed_both = json.loads(res_both.output)
                self.assertIn("up", parsed_both)
                self.assertIn("down", parsed_both)
                self.assertTrue((outdir / "summary_up.json").exists())
                self.assertTrue((outdir / "summary_down.json").exists())
                self.assertTrue((outdir / "summary_up.md").exists())
                self.assertTrue((outdir / "summary_down.md").exists())
