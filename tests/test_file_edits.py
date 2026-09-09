import json
import tempfile
import unittest
from pathlib import Path
from product_hunt_scraper.file_edits import queue_file_edits
from product_hunt_scraper.models import SourceProduct, SitemapEntry
from product_hunt_scraper.state import StateStore
from test_state import draft

class FileEditTests(unittest.TestCase):
    def test_republish_changed_existing_record_and_resume_without_reverting(self):
        with tempfile.TemporaryDirectory() as directory, StateStore(Path(directory)/'state.db') as state:
            entry=SitemapEntry('https://www.producthunt.com/products/original')
            state.enqueue([entry])
            state.mark_scraped(entry.url,SourceProduct('original',entry.url,'OriginalBrand'))
            state.mark_external_checked(entry.url,None)
            state.mark_transformed(entry.url,draft('Fresh Harbor'),'m','hash')
            original=state.transformed_records()[0]
            state.mark_published(entry.url,original['slug'])
            edited={**original,'name':'Another Harbor'}
            path=Path(directory)/'products.json'
            path.write_text(json.dumps([edited]))
            pending,report=queue_file_edits(state,path)
            self.assertEqual(pending,[entry])
            self.assertEqual(report['changed'],1)
            self.assertTrue(Path(report['backup']).exists())
            self.assertEqual(state.transformed_records()[0]['name'],'Another Harbor')
            self.assertEqual(state.transformed_records()[0]['slug'],original['slug'])
            self.assertEqual(queue_file_edits(state,path)[0],[entry])
            state.mark_published(entry.url,original['slug'])
            self.assertEqual(queue_file_edits(state,path)[0],[])
            self.assertEqual(json.loads(path.read_text()),[edited])

    def test_invalid_later_record_does_not_apply_earlier_edit(self):
        with tempfile.TemporaryDirectory() as directory, StateStore(Path(directory)/'state.db') as state:
            entry=SitemapEntry('https://www.producthunt.com/products/original')
            state.enqueue([entry])
            state.mark_scraped(entry.url,SourceProduct('original',entry.url,'OriginalBrand'))
            state.mark_external_checked(entry.url,None)
            state.mark_transformed(entry.url,draft('Fresh Harbor'),'m','hash')
            original=state.transformed_records()[0]
            path=Path(directory)/'products.jsonl'
            path.write_text(json.dumps({**original,'name':'Another Harbor'})+'\n'+json.dumps({'slug':'unknown'}))
            with self.assertRaisesRegex(ValueError,'unknown'):
                queue_file_edits(state,path)
            self.assertEqual(state.get_work_item(entry.url).draft.name,'Fresh Harbor')
