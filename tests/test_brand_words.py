import unittest
from dataclasses import replace
from product_hunt_scraper.gemini import transformation_issues
from product_hunt_scraper.models import SourceProduct
from test_state import draft

class BrandTests(unittest.TestCase):
    def test_generic_title_words_are_allowed_but_brand_is_rejected(self):
        for name, word in [('Sourclip: shared workspace', 'workspace'),
                           ('Computable GPU Index (CGI)', 'index'),
                           ('Atlas by World Labs', 'world'),
                           ('Plus AI Weekly Google Analytics Reports', 'weekly analytics reports'),
                           ('Claude Code', 'code'), ('Basedash: data platform', 'data'),
                           ('AI Search Console', 'search')]:
            source = SourceProduct('test', 'https://example.com', name)
            product = replace(draft('Signal Grove'), promise='Organize your '+word+' in one place.')
            self.assertEqual(transformation_issues(source, None, product), [], name)
        source = SourceProduct('test', 'https://example.com', 'Claude Code')
        product = replace(draft('Signal Grove'), promise='Organize Claude output in one place.')
        self.assertIn('source brand token appears in public copy: claude', transformation_issues(source, None, product))
