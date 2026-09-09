import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from product_hunt_scraper.batch import BatchGenerator, Ledger
from product_hunt_scraper.http import HttpError, HttpResponse
from product_hunt_scraper.models import SourceProduct


DRAFT = dict(
    name='LedgerBeacon',
    category='Receivables',
    audience='Freelance consultants',
    problem='Independent consultants lose track of outstanding client balances.',
    promise='Keep collection tasks organized.',
    differentiator='A focused queue highlights the next client balance to review.',
    workflow=['Add outstanding balances','Choose the next account','Record the follow up'],
    keywords=['billing','collections','invoices','reminders'],
    metrics=['Open balances','Time spent'],
    intentKey='invoice-operations',
)


def response(value):
    return HttpResponse(200,'https://example.com',{},json.dumps(value).encode())


def gemini(items,truncate=False):
    text = json.dumps({'items':items})
    if truncate:
        text = text[:-2]+', {"id":'
    return response({'candidates':[{'content':{'parts':[{'text':text}]}}]})


def job(identity):
    return {
        'id':identity,
        'source':SourceProduct(
            identity,
            'https://example.com/'+identity,
            'Original'+identity,
            tagline='Tools for invoice collection.',
            description='Users manage collection tasks for open customer balances.',
        ),
    }


class BatchLoggingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        clock = datetime(2026,9,8,12,tzinfo=timezone.utc).timestamp()
        self.ledger = Ledger(Path(self.tmp.name)/'ledger.db',clock=lambda:clock)
        self.ledger.rpd,self.ledger.rpm,self.ledger.tpm = 20,5,25000

    def tearDown(self):
        self.ledger.db.close()
        self.tmp.cleanup()

    def test_partial_response_logs_truncation_and_missing_ids(self):
        client = Mock()
        second = dict(DRAFT,name='BalanceTrail')
        client.post_json.side_effect = [
            gemini([{'id':'a','product':DRAFT}],truncate=True),
            gemini([{'id':'b','product':second}]),
        ]
        events = []
        original = client.post_json.side_effect
        def request(*args, **kwargs):
            events.append('request')
            return original[len([e for e in events if e == 'request']) - 1]
        original = list(original)
        client.post_json.side_effect = request
        generator = BatchGenerator(client,'key',['m'],self.ledger,batch_size=2,wait_minutes=0)
        generator.on_batch = lambda: events.append('publish')
        with self.assertLogs('product_hunt_scraper.batch',level='INFO') as logs:
            generator.generate([job('a'),job('b')],Mock(),lambda _:False)
        self.assertEqual(events, ['request', 'publish', 'request', 'publish'])
        output = '\n'.join(logs.output)
        self.assertIn('response JSON incomplete or malformed; complete items were salvaged',output)
        self.assertIn('missing from Gemini response=1',output)
        self.assertIn('1 unfinished will retry',output)

    def test_validation_rejection_logs_specific_reason(self):
        client = Mock()
        bad = dict(DRAFT,name='Originala Tracker')
        client.post_json.side_effect = [
            gemini([{'id':'a','product':bad}]),
            gemini([{'id':'a','product':DRAFT}]),
        ]
        with self.assertLogs('product_hunt_scraper.batch',level='DEBUG') as logs:
            BatchGenerator(client,'key',['m'],self.ledger,batch_size=1,wait_minutes=0).generate(
                [job('a')],Mock(),lambda _:False)
        output = '\n'.join(logs.output)
        self.assertIn('source brand appears in public copy=1',output)
        self.assertIn('Rejected a: source brand appears in public copy',output)

    def test_transport_failure_logs_actual_error_instead_of_http_none(self):
        client = Mock()
        client.post_json.side_effect = [
            HttpError('Request failed for Gemini: timed out'),
            gemini([{'id':'a','product':DRAFT}]),
        ]
        with self.assertLogs('product_hunt_scraper.batch',level='WARNING') as logs:
            BatchGenerator(client,'key',['slow','ready'],self.ledger,batch_size=1,wait_minutes=0).generate(
                [job('a')],Mock(),lambda _:False)
        output = '\n'.join(logs.output)
        self.assertIn('request failed before an HTTP response after',output)
        self.assertIn('timed out',output)
        self.assertNotIn('HTTP None',output)


if __name__ == '__main__':
    unittest.main()
