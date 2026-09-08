# Product Hunt → Magic Catalog

Slow direct scraping of Product Hunt's product sitemap and pages, fixed-batch Gemini generation, and scalable catalog publishing. No Product Hunt API or login is required.

## First-time setup (Windows PowerShell)

```powershell
git clone https://github.com/feet-code/product-hunt-scraper.git
cd product-hunt-scraper
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env
```

Python 3.11+ is required. On macOS/Linux use `python3 -m venv .venv`, `source .venv/bin/activate`, and `cp .env.example .env`.

The crawler discovers the public product sitemap, reports its total count, and selects the newest products by default. `--order sitemap` keeps sitemap order; `--offset` selects a later slice. There is no longer a hard 10,000-product ceiling.

Source requests remain sequential with a default 5–7 second delay and bounded Retry-After/backoff. `--product-hunt-delay` and `--product-hunt-jitter` can increase spacing. One external landing page is read when its robots policy allows, with public-network/redirect checks; `--no-external` skips that enrichment for faster scraping. Raw pages, images, comments, and maker profiles are not stored. Use direct scraping only for access you are authorized to make.

The checkpoint remains `.state/scraper.sqlite3`; existing state is migrated additively without discarding successful work. `--state` and `--preview` customize paths, and `--env-file` selects a different dotenv file. `export --output .state/review.jsonl` writes public catalog records from saved drafts without network requests.

## Upgrade to batching

```powershell
git pull
python -m pip install -e .
```

Keep your existing `.state` directory. Completed scrapes, generated records, and published products are retained. Existing Product Hunt drafts get a shared general-software intent when none was previously stored; they do not consume a Gemini call just to migrate. Already published legacy products remain published and are not copied into the new storage path.

Before publishing, update **magic-catalog** too:

```powershell
git pull
npm ci
npm run deploy
```

If scalable resources have never been created, first run `npm run scale:setup` in magic-catalog. Its updated ingest endpoint reports measured D1 writes. The importers check that capability before sending products and stop if the old version is deployed. They use **ADMIN_REINDEX_TOKEN**, not the legacy ADMIN_IMPORT_TOKEN.

## Three independent queues

```powershell
# Scrape only: no Gemini key or catalog token needed.
ph-magic-import run --stage scrape --limit 3

# Generate from saved sources: no source-site requests.
ph-magic-import run --stage generate --limit 3

# Publish saved products: no Gemini key or source-site requests.
ph-magic-import run --stage publish --limit 3
```

Review `.state/products.jsonl` after generation. These are compact public product records that Magic Catalog renders into pages, not HTML or copies of research pages.

Scale up by raising the total selected limit:

```powershell
ph-magic-import run --stage scrape --limit 100000
ph-magic-import run --stage generate --limit 100000
ph-magic-import run --stage publish --limit 100000
```

Or keep the one-command workflow:

```powershell
ph-magic-import run --limit 100000 --publish
```

`run` first scrapes the selected inventory, then generates from the checkpoint, then optionally publishes. Gemini quota exhaustion pauses generation, but any valid generated products are still eligible for publishing. Use `--stage scrape` whenever you want to continue collecting source data independently. The default limit is still 3. A larger limit is a ceiling, not a guarantee the source contains that many distinct accessible products.

`--scrape-only` is an alias for `--stage scrape`. `--offline` prevents source-site requests. Ctrl+C preserves completed stages; rerun the same command to resume. These are local CLI commands, not unattended scheduled jobs; daily quota resets do not restart a stopped process automatically.

## Fixed Gemini batches

Generation uses **50 products per request**, every full batch. It ignores the old saved adaptive size, and neither successful responses, partial validation failures, nor HTTP errors change the configured batch size. Only the final remainder (including unfinished retries) can contain fewer products. Set a different fixed size explicitly with `--batch-size` if needed. The old `--max-batch-size` option is accepted for compatibility but has no effect.

- HTTP 503 triggers fallback with the **same products and payload**, plus a persisted cooldown of at least five minutes (or a longer Retry-After). It indicates temporary service unavailability; it is not treated as daily quota exhaustion.
- Valid products are saved immediately. Failed validation items return to the end of the pending queue, joining other pending products in full batches where possible. Only unfinished items retry, up to three validation attempts per run.
- RPM, TPM, daily request limits, and persisted cooldowns still apply. If a full batch exceeds configured `GEMINI_TPM`, generation pauses before sending it; it does **not** silently split into smaller calls. Choose a smaller explicit batch size, or correct the TPM setting only if your AI Studio project actually permits more.
- Source-brand, copied-phrase, ID, and field validation remain enabled. No paid Batch API is used.

```powershell
ph-magic-import run --stage generate --limit 100000
```

### Generate and publish saved scrapes together

```powershell
ph-magic-import run --stage generate-publish --limit 100000
```

Equivalent: `run --stage generate --limit 100000 --publish`. Both use saved scrapes without opening the source site or rediscovering listings. They generate first, then publish available products; if generation pauses for quota or service availability, already generated products are still published. This is sequential, not concurrent. Ctrl+C preserves generated products; rerun to continue or use `--stage publish` to publish them directly.

`run --limit 100000 --publish` still uses the full scrape → generate → publish flow and can perform more discovery. Add `--offline` to that command to use saved sources only, or use the explicit `generate-publish` stage above.

## Shared, restart-safe quotas

Both repositories default to **the same SQLite ledger** at `~/.magic-catalog/quotas.sqlite3` (your user home directory). Running them on the same computer and OS user shares Gemini request accounting, cooldowns, intent registration, and publishing budgets. Each scraper still has its own `.state` research checkpoint.

Set these `.env` values to your actual AI Studio limits; defaults are conservative assumptions, not guaranteed Google quotas:

```text
GEMINI_RPD=20
GEMINI_RPM=5
GEMINI_TPM=25000
GEMINI_QUOTA_SCOPE=default-project
MAGIC_WRITE_SCOPE=cloudflare-account
```

The limits above apply per configured model. The input-token estimate uses serialized request size; server 429s remain authoritative. The ledger reserves attempts **before** sending them, including failed requests. It saves model cooldowns, honors Retry-After, treats recognized daily-quota errors as unavailable until midnight Pacific, and skips missing models for 24 hours. Quota changes and cooldowns survive Ctrl+C and restarts.

Temporary availability waits are bounded to two minutes by default, with short interruptible waits. Use `--wait-minutes 0` to stop immediately when no model is ready. Daily exhaustion prints the next eligible time and leaves unfinished items queued. Model/key errors do not poison every remaining source row.

```powershell
ph-magic-import quota-status
ph-magic-import status
ph-magic-import retry-failed
```

If you customize `MAGIC_QUOTA_DB`, give both repositories the **same absolute file path**. `GEMINI_QUOTA_SCOPE` identifies the actual Google project; do not change it merely to reset quotas. Two different API keys for the same project still need the same scope. Other programs and separate computers do not automatically share this ledger; leave headroom for their usage. Run at most one process per scraper checkpoint.

## Measured scalable publishing

Set the following in each scraper's `.env`:

```text
GEMINI_API_KEY=your-key
MAGIC_CATALOG_URL=https://magic-catalog.cloudwebsites.workers.dev
MAGIC_CATALOG_IMPORT_TOKEN=the-same-value-as-the-Worker-ADMIN_REINDEX_TOKEN
```

Publishing uses `/api/admin/catalog/ingest`: R2 product bodies, sharded D1 metadata and FTS, and **59 shared intent vectors**. It never creates a vector per imported product. Source research and credentials are not sent in public product records.

Generation batch size and import batch size are independent. The CLI permits an import ceiling of 25, but automatically honors the server's lower advertised limit, currently **7 products/request**. The current database statements plus intent and object writes need this smaller batch to leave room under the Worker Free request limits. Intent definitions are sent once per destination in the shared ledger.

The default publishing allowance is **80,000 D1 rows/day shared between both importers**, leaving nominal headroom below D1 Free's 100,000. This counts measured metadata, index, FTS, and intent writes reported by D1; it does not assume one product equals one row.

Before the first measured batch, reserve 100 rows per product. Afterwards estimate using the highest observed rows per product plus 50% and four extra rows. Successful batches reconcile reservations to actual reported usage. Failed/uncertain batches retain their conservative reservations; their exact saved products remain queued for idempotent retry. A missing acknowledgement or missing usage report never marks products as published. This is a local planning budget, not a guarantee against unobserved account traffic or an unexpectedly expensive query.

```powershell
ph-magic-import run --stage publish --limit 100000 --daily-row-budget 60000
```

The budget resets at midnight UTC. Other applications on the account consume the same Cloudflare free allowance, so lower this budget if necessary. Renamed products use stable source-derived slugs; replaying a partially successful batch upserts the same products. Preserve both the research checkpoint and shared ledger for reliable progress/quota accounting.

If vector indexing was unavailable, `npm run vector:reindex` in Magic Catalog repairs persisted intents. Neither scraper changes Cloudflare billing or provisions paid resources.

## Tests

```powershell
python -m unittest discover -s tests -v
```

Tests cover partial/truncated responses, wrong and duplicate IDs, fixed batch sizes and 503 fallback, model fallback, persisted daily/minute quotas, Pacific daylight-saving resets, intent reuse, server-advertised import caps, measured write accounting, partial failures, stable identities, and preservation of existing checkpoints.


## Scrape now, generate and publish later

```bash
# Save up to 1,000 sources first; no Gemini key needed for scraping.
ph-magic-import run --stage scrape --limit 1000

# Generate and publish pending saved sources, without contacting source sites.
ph-magic-import run --stage generate --publish --limit 100000

# Expand the selected sitemap inventory later, skipping completed scrapes.
ph-magic-import run --stage scrape --limit 2000
ph-magic-import run --stage generate --publish --limit 100000
```

Use the same `.state/scraper.sqlite3` file (or the same `--state` path) each time.
Generation reuses saved drafts and publishing skips acknowledged products. Saved-data
stages apply `--limit` to eligible pending records, so completed rows cannot hide
newly scraped work. An empty queue exits successfully. `--offset` on these stages
skips pending records; normally leave it at zero. Scrape limits still select a
slice of the sitemap, so increase that limit to collect more products.

Ctrl+C preserves each completed source, draft, and publish acknowledgment. If a
publish response is lost, the stored stable product identity makes the retry
idempotent. Generation finishes the selected pending batch before publishing;
if Gemini pauses on quota, valid saved drafts can still be published in that run.
