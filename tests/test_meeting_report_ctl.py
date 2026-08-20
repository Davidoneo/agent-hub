import importlib.machinery
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "libexec" / "meeting-report-ctl"
LOADER = importlib.machinery.SourceFileLoader("meeting_report_ctl", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
meeting_report_ctl = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(meeting_report_ctl)


class MeetingReportTests(unittest.TestCase):
    def test_canonicalize_separates_and_deduplicates_report_items(self):
        report = meeting_report_ctl.canonicalize({
            "summary": "  Esito  ",
            "decisions": [
                {"title": "Usare PostgreSQL", "detail": "Scelta esplicita",
                 "rationale": "Affidabilita'", "evidence": "[010.0 - 020.0]"},
                {"title": "  usare   postgresql  ", "detail": "  scelta   esplicita  "},
                {"title": "", "detail": ""},
            ],
            "open_questions": [{"title": "Regione cloud", "detail": "Da decidere"}],
            "actions": [{"title": "Preparare migrazione", "detail": "Bozza",
                         "owner": "Mario", "due_date": "2026-09-01"}],
            "ignored": "not allowed",
        })

        self.assertEqual(len(report["decisions"]), 1)
        self.assertEqual(report["decisions"][0]["evidence"], "[010.0 - 020.0]")
        self.assertEqual(report["open_questions"][0]["title"], "Regione cloud")
        self.assertEqual(report["actions"][0]["owner"], "Mario")
        self.assertNotIn("ignored", report)

    def test_minutes_make_open_questions_and_evidence_visible(self):
        text = meeting_report_ctl.minutes_markdown({
            "title": "Review",
            "project_slug": "demo",
            "meeting_date": "2026-08-19T10:00",
            "round": 2,
        }, {
            "summary": "Riepilogo",
            "decisions": [{"title": "Scelta", "detail": "Dettaglio",
                           "rationale": "Motivo", "evidence": "[1.0 - 2.0]"}],
            "open_questions": [{"title": "Aperta", "detail": "Da chiarire"}],
            "actions": [{"title": "Azione", "detail": "Fare",
                         "owner": "Luca", "due_date": "venerdi'"}],
        })

        self.assertIn("## Questioni aperte", text)
        self.assertIn("**Evidenza nella trascrizione:** [1.0 - 2.0]", text)
        self.assertIn("**Responsabile:** Luca", text)


if __name__ == "__main__":
    unittest.main()
