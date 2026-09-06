"""Unit tests for Microsoft plus-alias (no network)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from grokreg.mail import hotmail_alias as halt
from grokreg.mail.providers import HotmailProvider


class AliasHelpersTest(unittest.TestCase):
    def test_make_plus_alias(self) -> None:
        self.assertEqual(halt.make_plus_alias("user@hotmail.com", 0), "user@hotmail.com")
        self.assertEqual(halt.make_plus_alias("user@hotmail.com", 1), "user+1@hotmail.com")
        self.assertEqual(halt.make_plus_alias("user@hotmail.com", 4), "user+4@hotmail.com")

    def test_mailbox_from_alias(self) -> None:
        self.assertEqual(halt.mailbox_from_alias("user@hotmail.com"), "user@hotmail.com")
        self.assertEqual(halt.mailbox_from_alias("user+3@hotmail.com"), "user@hotmail.com")

    def test_alias_index_and_match(self) -> None:
        mb = "a@outlook.com"
        self.assertEqual(halt.alias_index_of("a@outlook.com", mb), 0)
        self.assertEqual(halt.alias_index_of("a+2@outlook.com", mb), 2)
        self.assertTrue(halt.alias_matches_mailbox("a+4@outlook.com", mb, 5))
        self.assertFalse(halt.alias_matches_mailbox("b+1@outlook.com", mb, 5))

    def test_clamp(self) -> None:
        self.assertEqual(halt.clamp_max_aliases("5"), 5)
        self.assertEqual(halt.clamp_max_aliases(0), 1)
        self.assertEqual(halt.clamp_max_aliases(99), 20)
        self.assertEqual(halt.max_aliases_from_config({}), 5)
        self.assertEqual(halt.max_aliases_from_config({"hotmail_max_aliases": 3}), 3)


class HotmailProviderAliasTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.list_path = self.dir / "hotmails.txt"
        self.list_path.write_text(
            "main@hotmail.com|pw|refresh|cid123\n",
            encoding="utf-8",
        )
        self.hp = HotmailProvider(self.list_path, max_aliases=5)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_acquire_then_mark_keeps_line_until_exhausted(self) -> None:
        seen: list[str] = []
        for i in range(5):
            sess = self.hp.acquire()
            seen.append(sess.address)
            self.assertEqual(sess.mailbox, "main@hotmail.com")
            self.assertEqual(sess.extra.get("alias_index"), i)
            self.hp.mark_used(sess)

        self.assertEqual(
            seen,
            [
                "main@hotmail.com",
                "main+1@hotmail.com",
                "main+2@hotmail.com",
                "main+3@hotmail.com",
                "main+4@hotmail.com",
            ],
        )
        leftover = self.list_path.read_text(encoding="utf-8").strip()
        self.assertEqual(leftover, "")
        used = (self.dir / "hotmails_used.txt").read_text(encoding="utf-8")
        self.assertIn("main@hotmail.com|pw|refresh|cid123", used)
        ledger = json.loads((self.dir / "hotmail_aliases.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["main@hotmail.com"]["used"], [0, 1, 2, 3, 4])

    def test_otp_timeout_does_not_burn_alias(self) -> None:
        first = self.hp.acquire()
        self.assertEqual(first.address, "main@hotmail.com")
        # no mark_used → same alias again
        again = self.hp.acquire()
        self.assertEqual(again.address, "main@hotmail.com")
        slots, lines = self.hp.available_count()
        self.assertEqual(lines, 1)
        self.assertEqual(slots, 5)


if __name__ == "__main__":
    unittest.main()
