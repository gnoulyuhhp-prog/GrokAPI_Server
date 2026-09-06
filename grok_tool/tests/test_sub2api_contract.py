import unittest

from grokreg.delivery.sub2api_client import Sub2APIClient, normalize_base_url


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = "json"

    def json(self):
        return self._payload


class RecordingSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class Sub2APIContractTest(unittest.TestCase):
    def test_normalize_base_url_removes_api_suffix(self):
        self.assertEqual(
            normalize_base_url("http://127.0.0.1:8080/api/v1/admin/"),
            "http://127.0.0.1:8080",
        )

    def test_import_sso_matches_sub2api_admin_contract(self):
        session = RecordingSession(
            [
                FakeResponse(
                    {
                        "code": 0,
                        "data": {
                            "created": [
                                {
                                    "index": 0,
                                    "name": "grok free 001",
                                    "email": "reg@example.com",
                                    "account": {"id": 42, "name": "grok free 001"},
                                }
                            ],
                            "failed": [],
                        },
                    }
                )
            ]
        )
        client = Sub2APIClient(
            "http://127.0.0.1:8080/api/v1",
            api_token="admin-token",
            session=session,
        )

        result = client.import_sso(
            "sso-cookie",
            email="reg@example.com",
            name="grok free 001",
            group_ids=[7],
        )

        self.assertEqual(result["account_id"], 42)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "http://127.0.0.1:8080/api/v1/admin/grok/sso-to-oauth")
        self.assertEqual(kwargs["headers"]["x-api-key"], "admin-token")
        self.assertEqual(kwargs["json"]["sso_tokens"], ["sso-cookie"])
        self.assertEqual(kwargs["json"]["group_ids"], [7])
        self.assertEqual(kwargs["json"]["name"], "grok free 001")
        self.assertNotIn("sso-cookie", repr(result))


if __name__ == "__main__":
    unittest.main()
