import unittest

from pydantic import ValidationError

from dba.config import Settings


class ProdSecretTest(unittest.TestCase):
    def test_dev_allows_placeholders(self):
        settings = Settings(_env_file=None, env="dev")
        self.assertEqual(settings.env, "dev")

    def test_prod_rejects_placeholders(self):
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, env="prod")

    def test_prod_accepts_configured_secrets(self):
        settings = Settings(
            _env_file=None,
            env="prod",
            mysql_dsn="mysql+asyncmy://user:pass@db:3306/dba",
            mysql_ro_dsn="mysql+asyncmy://reader:pass@db:3306/dba",
            redis_dsn="redis://:pass@redis:6379/0",
            jwt_secret="x" * 32,
        )
        self.assertEqual(settings.env, "prod")


if __name__ == "__main__":
    unittest.main()
