import os
import unittest
from unittest.mock import patch

from product_hunt_scraper.config import configured_gemini_api_keys


class GeminiKeyConfigTests(unittest.TestCase):
    def test_single_legacy_key_still_works(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "legacy", "GEMINI_API_KEYS": ""}, clear=False):
            self.assertEqual(configured_gemini_api_keys(), ("legacy",))

    def test_pool_is_deduplicated_and_legacy_key_is_appended(self):
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEYS": "alpha, beta,alpha", "GEMINI_API_KEY": "legacy"},
            clear=False,
        ):
            self.assertEqual(
                configured_gemini_api_keys(),
                ("alpha", "beta", "legacy"),
            )


if __name__ == "__main__":
    unittest.main()
