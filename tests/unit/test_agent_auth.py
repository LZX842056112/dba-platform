import unittest

from dba.api.agent_auth import verify_agent_token


class AgentTokenTest(unittest.TestCase):
    def test_matching_token(self):
        self.assertEqual(verify_agent_token("secret", "secret"), "secret")

    def test_missing_token(self):
        with self.assertRaises(ValueError):
            verify_agent_token(None, "secret")

    def test_wrong_token(self):
        with self.assertRaises(ValueError):
            verify_agent_token("wrong", "secret")

    def test_unconfigured_expected_token(self):
        with self.assertRaises(ValueError):
            verify_agent_token("secret", "")


if __name__ == "__main__":
    unittest.main()
