"""Known-name checks and durable repair preparation for existing checkpoints."""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .gemini import brand_identity, normalize_text, transformation_issues


def name_key(value):
    return normalize_text(value).replace(' ', '')


def blocked_names(state):
    names = {'alphasentinel', 'skillforge'}
    extra = os.getenv('BRAND_BLOCKLIST_FILE')
    if extra:
        names.update(name_key(line) for line in Path(extra).read_text(encoding='utf-8').splitlines()
                     if line.strip() and not line.lstrip().startswith('#'))
    for row in state.connection.execute('SELECT source_url, source_json FROM work_items'):
        names.add(name_key(urlsplit(row['source_url']).path.rstrip('/').rsplit('/', 1)[-1]))
        if row['source_json']:
            source = json.loads(row['source_json'])
            names.add(name_key(brand_identity(source['source_name'])))
    return names - {''}


def issues_for(item, names):
    if not item.draft:
        return []
    issues = transformation_issues(item.source, item.external_page, item.draft) if item.source else []
    if name_key(item.draft.name) in names:
        issues.append('replacement name matches a known source or blocked product')
    return issues


def audit(state, repair=False):
    names = blocked_names(state)
    flagged = []
    for row in state.connection.execute('SELECT source_url FROM work_items WHERE draft_json IS NOT NULL').fetchall():
        item = state.get_work_item(row['source_url'])
        issues = issues_for(item, names)
        if issues:
            flagged.append({'source_url': item.source_url, 'name': item.draft.name,
                            'status': item.status, 'issues': issues})
    backup = None
    if repair and flagged:
        backup = str(state.path) + '.before-brand-repair-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.sqlite3'
        with sqlite3.connect(backup) as destination:
            state.connection.backup(destination)
        with state.connection:
            for record in flagged:
                # Keep published_slug and scalable-product metadata: these are the remote identity.
                state.connection.execute("""UPDATE work_items SET draft_json=NULL,
                    draft_name_normalized=NULL, generation_model=NULL, source_content_hash=NULL,
                    status='scraped', attempts=0, failed_stage=NULL, error=NULL
                    WHERE source_url=?""", (record['source_url'],))
    return {'flagged': flagged, 'queued_for_repair': len(flagged) if repair else 0, 'backup': backup}
