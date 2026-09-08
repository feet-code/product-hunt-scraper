import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from datetime import datetime, timezone
from product_hunt_scraper.batch import Ledger, BatchGenerator, BulkPublisher, Paused, salvage_items, pacific_day, next_day
from product_hunt_scraper.http import HttpError, HttpResponse
from product_hunt_scraper.models import SourceProduct

DRAFT = dict(name='LedgerBeacon',category='Receivables',audience='Freelance consultants',
    problem='Independent consultants lose track of outstanding client balances.',promise='Keep collection tasks organized.',
    differentiator='A focused queue highlights the next client balance to review.',
    workflow=['Add outstanding balances','Choose the next account','Record the follow up'],
    keywords=['billing','collections','invoices','reminders'],metrics=['Open balances','Time spent'],intentKey='invoice-operations')


def response(value):
    return HttpResponse(200,'https://example.com',{},json.dumps(value).encode())


def gemini(items,truncate=False):
    text = json.dumps({'items':items})
    if truncate: text = text[:-2]+', {"id":'
    return response({'candidates':[{'content':{'parts':[{'text':text}]}}]})


def job(identity):
    return {'id':identity,'source':SourceProduct(identity,'https://example.com/'+identity,'Original'+identity,
        tagline='Tools for invoice collection.',description='Users manage collection tasks for open customer balances.')}


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = [datetime(2026,9,7,12,tzinfo=timezone.utc).timestamp()]
        self.ledger = Ledger(Path(self.tmp.name)/'ledger.db',clock=lambda:self.clock[0])
        self.ledger.rpd,self.ledger.rpm,self.ledger.tpm=20,5,25000

    def tearDown(self):
        self.ledger.db.close()
        self.tmp.cleanup()

    def test_preflight_cannot_redirect_admin_token(self):
        from urllib.request import Request
        from product_hunt_scraper.http import _ValidatingRedirectHandler
        with self.assertRaises(HttpError):
            _ValidatingRedirectHandler(False).redirect_request(
                Request('https://catalog.example',headers={'Authorization':'Bearer test'}),
                None,302,'',{},'https://other.example')

    def test_daily_quota_shared_between_processes_and_restart(self):
        self.ledger.rpd=1
        self.assertTrue(self.ledger.claim('m',100))
        other=Ledger(Path(self.tmp.name)/'ledger.db',clock=lambda:self.clock[0]);other.rpd=1
        try: self.assertFalse(other.claim('m',100))
        finally: other.db.close()
        self.clock[0]=next_day(self.clock[0])+1
        self.assertTrue(self.ledger.claim('m',100))

    def test_rpm_and_tokens_are_independent(self):
        self.ledger.rpm=1
        self.assertTrue(self.ledger.claim('m',100))
        self.assertFalse(self.ledger.claim('m',100))
        self.clock[0]+=61
        self.assertTrue(self.ledger.claim('m',24999))
        self.ledger.rpm=5
        self.assertFalse(self.ledger.claim('m',2))

    def test_429_daily_and_retry_after_survive(self):
        self.ledger.block('m',HttpError('quota',status=429,body='RequestsPerDay'))
        self.assertEqual(self.ledger.ready_at('m'),next_day(self.clock[0]))
        self.ledger.block('other',HttpError('rate',status=429,retry_after=120))
        self.assertEqual(self.ledger.ready_at('other'),self.clock[0]+120)

    def test_pacific_reset_handles_dst(self):
        before=datetime(2026,3,8,8,tzinfo=timezone.utc).timestamp()
        self.assertEqual(next_day(before)-before,23*3600)

    def test_salvages_complete_objects_from_truncation(self):
        self.assertEqual(salvage_items('{"items":[{"id":"a","product":{}},{"id":'),[{'id':'a','product':{}}])

    def test_partial_batch_saves_good_and_retries_only_failed(self):
        client=Mock()
        second=dict(DRAFT,name='BalanceTrail')
        client.post_json.side_effect=[gemini([{'id':'a','product':DRAFT}],True),gemini([{'id':'b','product':second}])]
        saved=[]
        engine=BatchGenerator(client,'key',['m'],self.ledger,batch_size=2,max_batch_size=10,wait_minutes=0)
        engine.generate([job('a'),job('b')],lambda j,p,m:saved.append((j['id'],p)),lambda _:False)
        self.assertEqual([s[0] for s in saved],['a','b'])
        research=json.loads(client.post_json.call_args_list[1].args[1]['contents'][0]['parts'][0]['text'])['research']
        self.assertEqual([r['id'] for r in research],['b'])

    def test_unknown_and_duplicate_ids_not_accepted(self):
        client=Mock();client.post_json.return_value=gemini([{'id':'a','product':DRAFT},{'id':'a','product':DRAFT},{'id':'foreign','product':DRAFT}])
        saved=[]
        with self.assertRaises(Paused):
            BatchGenerator(client,'key',['m'],self.ledger,1,1,0).generate([job('a')],lambda *args:saved.append(args),lambda _:False)
        self.assertEqual(saved,[])
        self.assertEqual(client.post_json.call_count,3)

    def test_fallback_and_cooldown_skip_exhausted_model(self):
        client=Mock();client.post_json.side_effect=[HttpError('quota',status=429,body='RequestsPerDay'),gemini([{'id':'a','product':DRAFT}])]
        saved=[]
        BatchGenerator(client,'key',['full','ready'],self.ledger,1,1,0).generate([job('a')],lambda *args:saved.append(args),lambda _:False)
        self.assertEqual(saved[0][2],'ready')
        self.assertFalse(self.ledger.claim('full',1))

    def test_all_models_exhausted_makes_no_network_call(self):
        self.ledger.rpd=1;self.ledger.claim('m',1)
        client=Mock()
        with self.assertRaises(Paused): BatchGenerator(client,'key',['m'],self.ledger,1,1,0).generate([job('a')],Mock(),lambda _:False)
        client.post_json.assert_not_called()

    def test_fixed_fifty_ignores_old_adaptive_checkpoint_and_validation_failure(self):
        self.ledger.set('adaptive:'+self.ledger.scope,2)
        self.ledger.tpm=1000000
        client=Mock(); sizes=[]; attempts=[0]
        def post(url,payload,**kwargs):
            research=json.loads(payload['contents'][0]['parts'][0]['text'])['research']
            sizes.append(len(research)); attempts[0]+=1
            # Save 49/50 on the first call; its failed item goes behind the next 50.
            items=research[1:] if attempts[0]==1 else research
            return gemini([{'id':r['id'],'product':DRAFT} for r in items])
        client.post_json.side_effect=post
        engine=BatchGenerator(client,'key',['m'],self.ledger,wait_minutes=0)
        saved=[]
        engine.generate([job(str(i)) for i in range(100)],lambda j,*rest:saved.append(j['id']),lambda _:False)
        self.assertEqual(sizes,[50,50,1]); self.assertEqual(len(set(saved)),100)
        self.assertEqual(engine.size,50)
        self.assertEqual(BatchGenerator(client,'key',['m'],self.ledger).size,50)

    def test_503_falls_back_with_identical_fifty_and_temporary_cooldown(self):
        self.ledger.tpm=1000000
        client=Mock()
        items=[{'id':str(i),'product':DRAFT} for i in range(50)]
        client.post_json.side_effect=[HttpError('busy',status=503,body='daily service unavailable',retry_after=400),gemini(items)]
        saved=[]
        engine=BatchGenerator(client,'key',['busy','ready'],self.ledger,wait_minutes=0)
        engine.generate([job(str(i)) for i in range(50)],lambda j,*rest:saved.append(j['id']),lambda _:False)
        self.assertEqual(client.post_json.call_args_list[0].args[1],client.post_json.call_args_list[1].args[1])
        self.assertEqual(len(saved),50);self.assertEqual(engine.size,50)
        self.assertEqual(self.ledger.ready_at('busy'),self.clock[0]+400)

    def test_oversized_fixed_batch_pauses_without_smaller_requests(self):
        self.ledger.tpm=1
        client=Mock();engine=BatchGenerator(client,'key',['m'],self.ledger,wait_minutes=0)
        with self.assertRaisesRegex(Paused,'Fixed batch of 50'):
            engine.generate([job(str(i)) for i in range(50)],Mock(),lambda _:False)
        self.assertEqual(engine.size,50);client.post_json.assert_not_called()

    def test_publish_uses_server_batch_limit_and_measured_writes(self):
        client=Mock();client.get.return_value=response({'usageReportingVersion':1,'maxProducts':2})
        def post(url,payload,**kwargs):
            return response({'ok':True,'products':len(payload['products']),'written':[{'slug':p['slug']} for p in payload['products']], 'usage':{'rowsWritten':12*len(payload['products'])}})
        client.post_json.side_effect=post
        products=[dict(DRAFT,slug='p-'+str(i)) for i in range(5)]
        saved=[]
        BulkPublisher(client,self.ledger,'https://catalog.example','secret').publish(products,saved.append)
        self.assertEqual(len(saved),5)
        self.assertEqual([len(c.args[1]['products']) for c in client.post_json.call_args_list],[2,2,1])
        self.assertEqual(self.ledger.db.execute('SELECT used FROM writes').fetchone()[0],60)
        self.assertEqual(client.post_json.call_args_list[1].args[1]['intents'],[])

    def test_publish_failure_keeps_budget_reservation_and_no_ack(self):
        client=Mock();client.get.return_value=response({'usageReportingVersion':1,'maxProducts':7})
        client.post_json.side_effect=HttpError('partial',status=500)
        ack=Mock()
        with self.assertRaises(Paused): BulkPublisher(client,self.ledger,'https://catalog.example','secret').publish([dict(DRAFT,slug='p1')],ack)
        ack.assert_not_called()
        self.assertEqual(self.ledger.db.execute('SELECT used FROM writes').fetchone()[0],100)

    def test_missing_usage_does_not_acknowledge(self):
        client=Mock();client.get.return_value=response({'usageReportingVersion':1,'maxProducts':7})
        client.post_json.return_value=response({'ok':True,'products':1,'written':[{'slug':'p1'}]})
        ack=Mock()
        with self.assertRaises(Paused): BulkPublisher(client,self.ledger,'https://catalog.example','secret').publish([dict(DRAFT,slug='p1')],ack)
        ack.assert_not_called()

    def test_shared_publish_budget_stops_before_request(self):
        client=Mock();client.get.return_value=response({'usageReportingVersion':1,'maxProducts':7})
        with self.assertRaises(Paused): BulkPublisher(client,self.ledger,'https://catalog.example','secret',row_budget=99).publish([dict(DRAFT,slug='p1')],Mock())
        client.post_json.assert_not_called()
