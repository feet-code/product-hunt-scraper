import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from product_hunt_scraper.batch import Ledger
from product_hunt_scraper.http import HttpError
from product_hunt_scraper.key_pool import GeminiKeyPoolLedger, PooledGeminiClient


class KeyPoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = [datetime(2026, 9, 9, 17, tzinfo=timezone.utc).timestamp()]
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.db", clock=lambda: self.clock[0])
        self.ledger.rpd = 20
        self.ledger.rpm = 5
        self.ledger.tpm = 25000

    def tearDown(self):
        self.ledger.db.close()
        self.tmp.cleanup()

    def test_round_robin_and_independent_daily_limits(self):
        self.ledger.rpd = 1
        pool = GeminiKeyPoolLedger(self.ledger, ["alpha-secret", "beta-secret"])
        try:
            self.assertTrue(pool.claim("m", 100))
            self.assertEqual(pool.active_api_key, "alpha-secret")
            self.assertTrue(pool.claim("m", 100))
            self.assertEqual(pool.active_api_key, "beta-secret")
            self.assertFalse(pool.claim("m", 100))
        finally:
            pool.close()

    def test_daily_429_only_cools_down_active_key(self):
        pool = GeminiKeyPoolLedger(self.ledger, ["alpha-secret", "beta-secret"])
        try:
            self.assertTrue(pool.claim("m", 100))
            self.assertEqual(pool.active_api_key, "alpha-secret")
            pool.block("m", HttpError("quota", status=429, body="RequestsPerDay"))
            self.assertTrue(pool.claim("m", 100))
            self.assertEqual(pool.active_api_key, "beta-secret")
        finally:
            pool.close()

    def test_503_cools_model_across_pool(self):
        pool = GeminiKeyPoolLedger(self.ledger, ["alpha-secret", "beta-secret"])
        try:
            self.assertTrue(pool.claim("m", 100))
            pool.block("m", HttpError("busy", status=503, retry_after=300))
            self.assertFalse(pool.claim("m", 100))
        finally:
            pool.close()

    def test_client_injects_selected_key_without_using_placeholder(self):
        underlying = Mock()
        underlying.max_attempts = 3
        pool = GeminiKeyPoolLedger(self.ledger, ["alpha-secret", "beta-secret"])
        try:
            self.assertTrue(pool.claim("m", 100))
            client = PooledGeminiClient(underlying, pool)
            client.max_attempts = 1
            client.post_json("https://example.com", {"x": 1}, headers={"x-goog-api-key": "placeholder"})
            self.assertEqual(underlying.max_attempts, 1)
            self.assertEqual(
                underlying.post_json.call_args.kwargs["headers"]["x-goog-api-key"],
                "alpha-secret",
            )
        finally:
            pool.close()

    def test_raw_keys_are_not_persisted(self):
        pool = GeminiKeyPoolLedger(self.ledger, ["alpha-secret", "beta-secret"])
        try:
            self.assertTrue(pool.claim("m", 100))
        finally:
            pool.close()
        contents = Path(self.tmp.name, "ledger.db").read_bytes()
        self.assertNotIn(b"alpha-secret", contents)
        self.assertNotIn(b"beta-secret", contents)


if __name__ == "__main__":
    unittest.main()
