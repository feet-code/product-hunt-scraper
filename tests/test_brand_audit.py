import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from product_hunt_scraper.brand_audit import audit, blocked_names, name_key
from product_hunt_scraper.bulk import publish_saved
from product_hunt_scraper.models import SourceProduct, SitemapEntry
from product_hunt_scraper.state import StateStore
from test_state import draft

class AuditTests(unittest.TestCase):
    def test_published_collision_repair_preserves_remote_identity_and_backup(self):
        with tempfile.TemporaryDirectory() as directory, StateStore(Path(directory)/'state.db') as state:
            entry = SitemapEntry('https://www.producthunt.com/products/original')
            state.enqueue([entry])
            state.mark_scraped(entry.url,SourceProduct('original',entry.url,'OriginalBrand'))
            state.mark_external_checked(entry.url,None)
            state.mark_transformed(entry.url,draft('Alpha Sentinel'),'m','hash')
            old = {**draft('Alpha Sentinel').to_dict(),'slug':'ph-existing','id':'saved-id','createdAt':'2026-01-01T00:00:00Z'}
            state.set_metadata('scalable-product:'+entry.url,json.dumps(old))
            state.mark_published(entry.url,old['slug'])
            self.assertEqual(audit(state)['flagged'][0]['name'],'Alpha Sentinel')
            report = audit(state,repair=True)
            self.assertTrue(Path(report['backup']).exists())
            self.assertIsNone(state.get_work_item(entry.url).draft)
            state.mark_transformed(entry.url,draft('Fresh Harbor'),'m','hash')
            published=[]
            def publish(products, ack, batch):
                published.extend(products)
                for p in products: ack(p)
            with patch('product_hunt_scraper.bulk.BulkPublisher') as publisher:
                publisher.return_value.publish.side_effect=publish
                publish_saved(SimpleNamespace(state=state),[entry],
                    SimpleNamespace(daily_row_budget=80000,publish_batch_size=7),
                    SimpleNamespace(magic_catalog_url='https://example.com',magic_catalog_import_token='test'),Mock(),Mock())
            self.assertEqual(published[0]['slug'],old['slug'])
            self.assertEqual(published[0]['id'],old['id'])
            self.assertEqual(published[0]['name'],'Fresh Harbor')
            self.assertEqual(state.get_work_item(entry.url).status,'published')
            self.assertEqual(audit(state)['flagged'],[])

    def test_cross_source_names_and_cached_drafts_are_blocked(self):
        with tempfile.TemporaryDirectory() as directory, StateStore(Path(directory)/'state.db') as state:
            a,b = [SitemapEntry('https://www.producthunt.com/products/'+s) for s in ('different','other')]
            state.enqueue([a,b])
            for entry,name in [(a,'SomeBrand'),(b,'UniqueBrand: helpful tool')]:
                state.mark_scraped(entry.url,SourceProduct('id',entry.url,name))
                state.mark_external_checked(entry.url,None)
            self.assertIn(name_key('Unique Brand'),blocked_names(state))
            state.mark_transformed(a.url,draft('SkillForge'),'m','hash')
            with patch('product_hunt_scraper.bulk.BulkPublisher') as publisher:
                with self.assertRaisesRegex(RuntimeError,'Blocked unsafe saved draft'):
                    publish_saved(SimpleNamespace(state=state),[a],Mock(),Mock(),Mock(),Mock())
                publisher.assert_not_called()
