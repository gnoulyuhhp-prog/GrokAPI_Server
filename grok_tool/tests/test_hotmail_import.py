"""Parse pasted Hotmail / Outlook dumps."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from grokreg.mail.hotmail_import import parse_hotmail_text, format_line


class HotmailImportTest(unittest.TestCase):
    def test_pipe_grok_tool(self) -> None:
        out = parse_hotmail_text("a@hotmail.com|pw|REFRESHTOKENVALUE12345678901234567890|cid-guid")
        self.assertEqual(out["ok"], 1)
        row = out["rows"][0]
        self.assertEqual(row["email"], "a@hotmail.com")
        self.assertEqual(row["password"], "pw")
        self.assertTrue(row["refresh"].startswith("REFRESH"))

    def test_register_web_dashes(self) -> None:
        guid = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
        token = "M.C530_BAY." + ("x" * 40)
        out = parse_hotmail_text(f"b@outlook.com----secret----{guid}----{token}")
        self.assertEqual(out["ok"], 1)
        row = out["rows"][0]
        self.assertEqual(row["client_id"], guid)
        self.assertEqual(row["refresh"], token)

    def test_skip_blank_and_dupes(self) -> None:
        text = """
        # comment
        a@hotmail.com|pw
        a@hotmail.com|other
        not-an-email|pw
        """
        out = parse_hotmail_text(text)
        self.assertEqual(out["ok"], 1)
        self.assertEqual(out["invalid"], 2)

    def test_format_line(self) -> None:
        self.assertEqual(format_line("a@b.com", "p", "r", "c"), "a@b.com|p|r|c")

    def test_colon_mail_pass(self) -> None:
        out = parse_hotmail_text("c@hotmail.com:Secret123!")
        self.assertEqual(out["ok"], 1)
        self.assertEqual(out["rows"][0]["password"], "Secret123!")

    def test_semicolon_and_quotes(self) -> None:
        out = parse_hotmail_text('"d@outlook.com";"pw";"REFRESHTOKENVALUE12345678901234567890"')
        self.assertEqual(out["ok"], 1)
        self.assertEqual(out["rows"][0]["email"], "d@outlook.com")

    def test_numbered_prefix(self) -> None:
        out = parse_hotmail_text("1. e@live.com|pw")
        self.assertEqual(out["ok"], 1)
        self.assertEqual(out["rows"][0]["email"], "e@live.com")


if __name__ == "__main__":
    unittest.main()
