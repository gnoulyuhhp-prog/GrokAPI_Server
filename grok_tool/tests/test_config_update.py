import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from web_console.app import ConfigUpdateBody, update_config


class ConfigUpdateTest(unittest.TestCase):
    def payload(self, **overrides):
        data = {
            "sub2api": {
                "enabled": True,
                "mode": "auto",
                "url": "http://localhost:9000",
                "group": "team",
                "name_prefix": "nexus",
                "user": "operator@example.com",
                "password": None,
                "api_token": None,
            },
            "google_sheets": {
                "enabled": True,
                "spreadsheet_id": "sheet-2",
                "webapp_url": None,
            },
            "force_guest_on_start": False,
            "open_grok_after_success": True,
            "fixed_password": None,
        }
        data.update(overrides)
        return ConfigUpdateBody(**data)

    def test_update_preserves_secrets_and_unknown_keys(self):
        original = {
            "unknown_setting": 42,
            "fixed_password": "fixed-secret",
            "sub2api": {"sub2api_pass": "secret", "sub2api_api_token": "token"},
            "google_sheets": {"webapp_url": "https://secret-webapp"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(json.dumps(original), encoding="utf-8")
            with patch("web_console.app.ROOT", root):
                result = update_config(self.payload())
            saved = json.loads((root / "config.json").read_text(encoding="utf-8"))

        self.assertTrue(result["ok"])
        self.assertEqual(saved["unknown_setting"], 42)
        self.assertEqual(saved["fixed_password"], "fixed-secret")
        self.assertEqual(saved["sub2api"]["sub2api_pass"], "secret")
        self.assertEqual(saved["sub2api"]["sub2api_api_token"], "token")
        self.assertEqual(saved["google_sheets"]["webapp_url"], "https://secret-webapp")
        self.assertEqual(saved["sub2api"]["sub2api_url"], "http://localhost:9000")

    def test_invalid_mode_is_rejected_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "config.json"
            cfg.write_text('{"sentinel": true}', encoding="utf-8")
            body = self.payload()
            body.sub2api.mode = "invalid"
            with patch("web_console.app.ROOT", root), self.assertRaises(HTTPException):
                update_config(body)
            self.assertEqual(cfg.read_text(encoding="utf-8"), '{"sentinel": true}')


if __name__ == "__main__":
    unittest.main()
