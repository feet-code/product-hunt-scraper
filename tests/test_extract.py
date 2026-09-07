import gzip
import unittest

from product_hunt_scraper.extract import (
    clean_tracking_url,
    parse_product_hunt_page,
    parse_product_sitemap,
)


class ExtractTests(unittest.TestCase):
    def test_parses_gzipped_product_sitemap(self):
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://www.producthunt.com/products/acme-beacon</loc><lastmod>2026-01-02T03:04:05Z</lastmod></url>
          <url><loc>https://www.producthunt.com/about</loc></url>
        </urlset>"""
        entries = parse_product_sitemap(gzip.compress(xml))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].url, "https://www.producthunt.com/products/acme-beacon")
        self.assertEqual(entries[0].last_modified, "2026-01-02T03:04:05Z")

    def test_extracts_product_signals_and_external_link(self):
        html = """
        <html><head>
          <meta name="description" content="Organize customer evidence without losing follow-ups.">
          <script type="application/ld+json">
          {"@context":"https://schema.org","@id":"https://www.producthunt.com/products/acme-beacon","@type":["Product","WebApplication"],"name":"Acme Beacon","description":"A customer research system for product teams.","applicationCategory":"Customer research"}
          </script>
        </head><body>
          <h1>Acme Beacon</h1>
          <a data-test="visit-website-button" href="https://acme.example/start?ref=producthunt&amp;utm_source=ph&amp;plan=free">Visit website</a>
        </body></html>
        """
        product = parse_product_hunt_page(
            html,
            source_url="https://www.producthunt.com/products/acme-beacon",
        )
        self.assertEqual(product.external_id, "acme-beacon")
        self.assertEqual(product.source_name, "Acme Beacon")
        self.assertEqual(product.description, "A customer research system for product teams.")
        self.assertEqual(product.categories, ["Customer research"])
        self.assertEqual(product.website_url, "https://acme.example/start?plan=free")

    def test_tracking_cleaner_preserves_functional_query_parameters(self):
        self.assertEqual(
            clean_tracking_url(
                "https://example.com/demo?utm_campaign=launch&workspace=blue#pricing"
            ),
            "https://example.com/demo?workspace=blue",
        )


if __name__ == "__main__":
    unittest.main()

