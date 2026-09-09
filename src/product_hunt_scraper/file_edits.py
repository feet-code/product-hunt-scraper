"""Validate and queue edits to exported products without changing their identities."""
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from .brand_audit import blocked_names, issues_for, name_key
from .gemini import REQUIRED_FIELDS, validate_draft
from .intents import INTENTS
from .models import CatalogDraft, SitemapEntry
from .state import normalized_name


def load_records(path):
    text = path.read_text(encoding='utf-8-sig')
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(value, dict):
        value = value.get('products', [value])
    if not isinstance(value, list) or not value:
        raise ValueError('Input must be a nonempty JSON product array, {"products": [...]}, or JSONL export.')
    return value


def queue_file_edits(state, path):
    records = load_records(path)
    # Export creates/preserves the stable metadata used to map public slugs to source rows.
    state.transformed_records()
    lookup = {}
    for row in state.connection.execute("SELECT key,value FROM metadata WHERE key LIKE 'scalable-product:%'"):
        payload = json.loads(row['value'])
        lookup[payload['slug']] = (row['key'][len('scalable-product:'):], payload)
    known = blocked_names(state)
    prepared, seen = [], set()
    for index, record in enumerate(records, 1):
        if not isinstance(record, dict) or record.get('slug') not in lookup:
            raise ValueError(f'Record {index}: slug is missing or unknown to this checkpoint. Preserve exported slugs and use the original --state.')
        slug = record['slug']
        if slug in seen:
            raise ValueError(f'Duplicate input slug: {slug}')
        seen.add(slug)
        source_url, original = lookup[slug]
        if any(record.get(key, original.get(key)) != original.get(key) for key in ('id', 'createdAt')):
            raise ValueError(f'{slug}: preserve exported id and createdAt.')
        allowed = REQUIRED_FIELDS | {'intentKey', 'id', 'slug', 'createdAt'}
        if set(record) - allowed:
            raise ValueError(f'{slug}: unsupported fields: {sorted(set(record) - allowed)}')
        draft = validate_draft({k:record[k] for k in REQUIRED_FIELDS if k in record})
        intent = record.get('intentKey', original.get('intentKey'))
        if intent not in INTENTS:
            raise ValueError(f'{slug}: invalid intentKey.')
        draft = replace(draft, intentKey=intent)
        item = state.get_work_item(source_url)
        issues = issues_for(replace(item, draft=draft), known)
        if issues:
            raise ValueError(f'{slug}: ' + '; '.join(issues))
        product = {**original, **draft.to_dict()}
        prepared.append((source_url, item, draft, product, original))
    # Check final names across the whole checkpoint, accounting for all edits together.
    edits = {url: draft for url, _, draft, _, _ in prepared}
    names = {}
    for row in state.connection.execute('SELECT source_url,draft_json FROM work_items WHERE draft_json IS NOT NULL'):
        url = row['source_url']
        draft = edits.get(url) or CatalogDraft.from_dict(json.loads(row['draft_json']))
        key = name_key(draft.name)
        if key in names and (url in edits or names[key] in edits):
            raise ValueError('Duplicate final product name: '+draft.name)
        names[key] = url
    changed = [entry for entry in prepared if entry[3] != entry[4]]
    backup = None
    if changed:
        backup = str(state.path)+'.before-file-edits-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.sqlite3'
        with sqlite3.connect(backup) as destination:
            state.connection.backup(destination)
        with state.connection:
            # Clear names first to support atomic name swaps.
            for url, _, _, _, _ in changed:
                state.connection.execute('UPDATE work_items SET draft_name_normalized=NULL WHERE source_url=?',(url,))
            for url, _, draft, product, _ in changed:
                state.connection.execute("UPDATE work_items SET draft_json=?,draft_name_normalized=?,status='transformed',attempts=0,error=NULL,failed_stage=NULL WHERE source_url=?",
                    (json.dumps(draft.to_dict()),normalized_name(draft.name),url))
                state.connection.execute('UPDATE metadata SET value=? WHERE key=?',
                    (json.dumps(product), 'scalable-product:'+url))
    pending = [SitemapEntry(url) for url, item, _, product, original in prepared
               if product != original or item.status != 'published']
    return pending, {'input_records':len(records),'changed':len(changed),'pending':len(pending),'backup':backup}
