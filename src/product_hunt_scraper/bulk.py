from __future__ import annotations
import hashlib
import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from .batch import BatchGenerator, BulkPublisher, Ledger, Paused, stable_product
from .gemini import source_content_hash
from .models import CatalogDraft, SitemapEntry
from .pipeline import write_preview

LOG = logging.getLogger(__name__)


def saved_entries(state, limit, offset=0, *, stage="all", publish=False, max_failures=3):
    # Filter completed and ineligible work before LIMIT so it cannot starve new rows.
    ready = "source_json IS NOT NULL AND external_checked = 1 AND draft_json IS NULL AND attempts < ?"
    if stage == "publish":
        condition, parameters = "draft_json IS NOT NULL", []
    elif stage == "generate" and not publish:
        condition, parameters = ready, [max_failures]
    else:
        condition, parameters = f"(draft_json IS NOT NULL OR ({ready}))", [max_failures]
    rows = state.connection.execute(
        f"SELECT source_url, last_modified FROM work_items WHERE status != 'published' AND ({condition}) "
        "ORDER BY ordinal, source_url LIMIT ? OFFSET ?",
        (*parameters, limit, offset),
    ).fetchall()
    return [SitemapEntry(row['source_url'], row['last_modified']) for row in rows]


def run_bulk(pipeline,entries,args,settings,gemini_client,publish_client):
    state = pipeline.state
    errors = 0
    if args.stage in ('all','scrape') and not args.offline:
        for index,entry in enumerate(entries,1):
            item = state.get_work_item(entry.url)
            if item.status=='published' or item.attempts>=args.max_failures:
                continue
            stage = 'scrape-product-hunt'
            try:
                source = item.source
                if source is None:
                    LOG.info('[%d/%d] Scraping %s',index,len(entries),entry.url)
                    source = pipeline._scrape_source(entry.url,entry.last_modified)
                    state.mark_scraped(entry.url,source)
                stage = 'scrape-external'
                if not item.external_checked:
                    external = pipeline._external_page(source)
                    state.mark_external_checked(entry.url,external)
            except Exception as error:
                state.mark_failed(entry.url,stage,type(error).__name__)
                errors += 1
    ledger = Ledger()
    paused = False
    try:
        if args.stage in ('all','generate','generate-publish'):
            jobs = []
            for entry in entries:
                item = state.get_work_item(entry.url)
                if item.source and item.external_checked and not item.draft and item.status!='published' and item.attempts<args.max_failures:
                    jobs.append({'id':entry.url,'source':item.source,'external':item.external_page})
            if jobs:
                transformer = BatchGenerator(gemini_client,settings.gemini_api_key,settings.gemini_models,ledger,
                    args.batch_size,args.max_batch_size,args.wait_minutes)
                def save(job,product,model):
                    draft = CatalogDraft.from_dict(product)
                    state.mark_transformed(job['id'],draft,model,source_content_hash(job['source'],job['external']))
                try:
                    transformer.generate(jobs,save,lambda name:state.draft_name_exists(name,''))
                except Paused as error:
                    LOG.warning('%s',error)
                    paused = True
        if args.stage in ('publish','generate-publish') or (args.stage in ('all','generate') and args.publish):
            products = []
            identities = {}
            for entry in entries:
                item = state.get_work_item(entry.url)
                if not item.draft or item.status=='published':
                    continue
                key = 'scalable-product:'+entry.url
                stored = state.connection.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
                if stored:
                    product = json.loads(stored[0])
                else:
                    product = stable_product(item.draft.to_dict(),entry.url,'ph',datetime.now(timezone.utc).isoformat().replace('+00:00','Z'))
                    state.set_metadata(key,json.dumps(product,separators=(',',':')))
                products.append(product)
                identities[product['slug']] = entry.url
            if products:
                publisher = BulkPublisher(publish_client,ledger,settings.magic_catalog_url,settings.magic_catalog_import_token,args.daily_row_budget)
                publisher.publish(products,lambda p:state.mark_published(identities[p['slug']],p['slug']),args.publish_batch_size)
    finally:
        ledger.db.close()
        write_preview(settings.preview_path,state.transformed_records())
    print(json.dumps({'checkpoint':state.status_counts(),'selected':len(entries),'generation_paused':paused},indent=2))
    if not entries:
        LOG.info('No pending eligible products for this stage. Scrape more sources or check status/retry-failed.')
    return 1 if errors or paused else 0
