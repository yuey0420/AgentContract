import unittest

from pactflow.core.logger import sanitize_log_value


class TestLogSanitization(unittest.TestCase):
    def test_redacts_secret_fields_recursively(self):
        value = sanitize_log_value({"args": {"api_key": "secret", "nested": {"token": "abc"}}})
        self.assertEqual(value["args"]["api_key"], "[REDACTED]")
        self.assertEqual(value["args"]["nested"]["token"], "[REDACTED]")

    def test_replaces_content_with_fingerprint(self):
        value = sanitize_log_value({"args": {"content": "private text"}})
        fingerprint = value["args"]["content"]
        self.assertTrue(fingerprint["redacted"])
        self.assertEqual(fingerprint["length"], 12)
        self.assertNotIn("private text", str(fingerprint))


if __name__ == "__main__":
    unittest.main()
