import gzip
import unittest

from product_hunt_scraper.http import HttpError, PoliteHttpClient


class HttpTests(unittest.TestCase):
    def test_bounds_decompressed_responses(self) -> None:
        compressed = gzip.compress(b"a" * 2_000)
        self.assertEqual(
            PoliteHttpClient._decode_content(compressed, "gzip", 2_000),
            b"a" * 2_000,
        )
        with self.assertRaises(HttpError):
            PoliteHttpClient._decode_content(compressed, "gzip", 1_999)

    def test_rejects_unadvertised_content_encoding(self) -> None:
        with self.assertRaises(HttpError):
            PoliteHttpClient._decode_content(b"encoded", "br", 100)


if __name__ == "__main__":
    unittest.main()
