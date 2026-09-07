import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
from product_hunt_scraper.bulk import run_bulk
from product_hunt_scraper.models import SourceProduct,SitemapEntry
from product_hunt_scraper.state import StateStore
from test_state import draft

class StageTests(unittest.TestCase):
    def test_legacy_draft_exports_and_publishes_without_generation(self):
        with tempfile.TemporaryDirectory() as directory, StateStore(Path(directory)/'state.db') as state:
            entry=SitemapEntry('https://www.producthunt.com/products/one')
            state.enqueue([entry]);state.mark_scraped(entry.url,SourceProduct('one',entry.url,'SourceBrand'))
            state.mark_external_checked(entry.url,None)
            state.mark_transformed(entry.url,draft('Signal Grove'),'old-model','hash')
            records=state.transformed_records()
            self.assertEqual(records[0]['intentKey'],'general-software-utilities')
            self.assertNotIn('sourceName',records[0])
            self.assertEqual(records[0]['slug'],state.transformed_records()[0]['slug'])
            args=SimpleNamespace(stage='publish',offline=True,max_failures=3,publish=True,publish_batch_size=25,daily_row_budget=80000)
            settings=SimpleNamespace(magic_catalog_url='https://catalog.example',magic_catalog_import_token='test',preview_path=Path(directory)/'preview.jsonl')
            publisher=Mock()
            def publish(products,ack,batch_size):
                self.assertEqual(products[0]['slug'],records[0]['slug']);ack(products[0])
            publisher.publish.side_effect=publish
            with patch('product_hunt_scraper.bulk.Ledger',return_value=Mock()),patch('product_hunt_scraper.bulk.BulkPublisher',return_value=publisher),patch('product_hunt_scraper.bulk.BatchGenerator') as generate:
                self.assertEqual(run_bulk(SimpleNamespace(state=state),[entry],args,settings,Mock(),Mock()),0)
                generate.assert_not_called()
                self.assertEqual(state.get_work_item(entry.url).status,'published')

    def test_scraping_needs_no_gemini_key_and_finishes_sources_first(self):
        with tempfile.TemporaryDirectory() as directory, StateStore(Path(directory)/'state.db') as state:
            entry=SitemapEntry('https://www.producthunt.com/products/one');state.enqueue([entry])
            pipeline=SimpleNamespace(state=state,_scrape_source=lambda url,last:SourceProduct('one',url,'SourceBrand'),_external_page=lambda source:None)
            args=SimpleNamespace(stage='scrape',offline=False,max_failures=3,publish=False)
            settings=SimpleNamespace(preview_path=Path(directory)/'preview.jsonl')
            with patch('product_hunt_scraper.bulk.Ledger',return_value=Mock()),patch('product_hunt_scraper.bulk.BatchGenerator') as generate:
                self.assertEqual(run_bulk(pipeline,[entry],args,settings,Mock(),Mock()),0)
                generate.assert_not_called();self.assertIsNotNone(state.get_work_item(entry.url).source)
