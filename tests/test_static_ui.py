#!/usr/bin/env python3
"""Small contracts for the navigation and two-axis session status UI."""

import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NavParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_main_nav = False
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "nav" and values.get("id") == "nav":
            self.in_main_nav = True
        elif tag == "a" and self.in_main_nav:
            self.hrefs.append(values.get("href"))

    def handle_endtag(self, tag):
        if tag == "nav" and self.in_main_nav:
            self.in_main_nav = False


class StaticUiContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "app/static/style.css").read_text(encoding="utf-8")
        cls.index = (ROOT / "app/static/index.html").read_text(encoding="utf-8")

    def test_meetings_are_nested_under_projects(self):
        parser = NavParser()
        parser.feed(self.index)
        self.assertNotIn("#/meetings", parser.hrefs)
        self.assertIn('tabLink("meetings",', self.app)
        self.assertIn("?tab=meetings", self.app)

    def test_open_session_and_turn_outcome_are_separate(self):
        self.assertIn("function sessionIsOpen", self.app)
        self.assertIn('label = "Turno completato"', self.app)
        self.assertIn("const open = d.sessions.filter(sessionIsOpen)", self.app)
        self.assertIn("WAITING_SESSION", self.app)

    def test_project_tiles_and_nested_tabs_are_styled(self):
        self.assertIn(".project-tile {", self.css)
        self.assertIn(".project-tabs {", self.css)
        self.assertIn(".project-panel.active", self.css)


if __name__ == "__main__":
    unittest.main()
