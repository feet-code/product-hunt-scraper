# Product Hunt → Magic Catalog importer

A slow, restart-safe pipeline that discovers Product Hunt product pages, reads the linked product website when that site's robots policy permits it, asks Gemini to create an independently written and renamed catalog record, and optionally publishes the result to Magic Catalog's D1 database and Vectorize index.

This project scrapes Product Hunt directly; it does not use the Product Hunt API. Use it only for access you are authorized to make.

## What it does

1. Downloads Product Hunt's public `product_about_sitemap.xml.gz` sitemap.
2. Selects up to 10,000 `/products/<slug>` pages, newest-updated first by default.
3. Fetches Product Hunt pages sequentially with a 5–7 second delay by default.
4. Extracts the product name, descriptions, categories, and `Visit website` destination from server-rendered metadata.
5. Checks the external site's `robots.txt`, rejects private/local network destinations, and reads one external landing page when allowed.
6. Tries Gemini models in this exact fallback order:

   - `gemini-3.8-flash`
   - `gemini-3.7-flash`
   - `gemini-3.6-flash`
   - `gemini-3.5-flash`
   - `gemini-3-flash`
   - `gemini-2.5-flash`

7. Rejects output that contains the source brand, a confusingly similar new name, a duplicate generated name, or an exact seven-word source phrase.
8. Saves every completed stage in SQLite and can publish idempotent batches to Magic Catalog.

The source name and URLs are sent only to Magic Catalog's protected import endpoint for validation and deduplication; that endpoint hashes every source identifier before D1 persistence. They are not included in the public product record. The local ignored SQLite checkpoint retains the research needed to resume. Raw pages, images, reviews, comments, and maker profiles are not stored.

## Setup

Requires Python 3.11 or newer.

### Windows PowerShell

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env
```

### macOS or Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

Put your Gemini key in `.env`:

```text
GEMINI_API_KEY=...
```

No Product Hunt credentials or cookies are used.

## Test three products first

This scrapes and transforms three products but does not change Magic Catalog:

```bash
ph-magic-import run --limit 3
```

Review the generated JSONL at `.state/products.jsonl`, then inspect progress at any time:

```bash
ph-magic-import status
```

The SQLite checkpoint is `.state/scraper.sqlite3`. Interrupting with Ctrl+C is safe; run the same command again to continue from the last completed stage.

## Publish to Magic Catalog

The matching Magic Catalog change adds `/api/admin/import-products`. Deploy that change, set a separate secret on the Magic Catalog Worker, and use the same value in this repository:

```bash
# Run in the magic-catalog repository.
npx wrangler secret put ADMIN_IMPORT_TOKEN
npm run deploy
```

```text
# product-hunt-scraper/.env
MAGIC_CATALOG_URL=https://magic-catalog.cloudwebsites.workers.dev
MAGIC_CATALOG_IMPORT_TOKEN=the-same-long-random-value
```

Publish the initial test records:

```bash
ph-magic-import run --limit 3 --publish
```

Then expand the same restart-safe run to as many as 10,000:

```bash
ph-magic-import run --limit 10000 --publish
```

Already published source products are skipped. If D1 succeeds but Vectorize temporarily fails, Magic Catalog returns a retryable error; rerunning the same command safely retries indexing without duplicating the D1 product.

## Useful commands

```bash
# Keep sitemap order instead of sorting by last-modified time.
ph-magic-import run --limit 10 --order sitemap

# Process a later slice.
ph-magic-import run --offset 1000 --limit 100 --publish

# Export all transformed records again without network requests.
ph-magic-import export --output .state/review.jsonl

# Allow failed items to be attempted again without losing successful stages.
ph-magic-import retry-failed
ph-magic-import run --limit 10000 --publish
```

## Polite crawling defaults

- Exactly one request is in flight at a time.
- Product Hunt requests wait at least 5 seconds plus 0–2 seconds of jitter.
- External-site requests wait at least 2 seconds plus 0–1 second of jitter per host.
- HTTP 429 and temporary server errors honor `Retry-After` and use exponential backoff.
- Product Hunt pages, external pages, and sitemap responses have strict size limits.
- The crawler never signs in, solves challenges, rotates identities, or bypasses access controls.
- External redirects and DNS results are checked to prevent private-network requests.

The delays can be increased from the command line. Product Hunt delay values below two seconds are clamped to two seconds:

```bash
ph-magic-import run --limit 25 --product-hunt-delay 8 --product-hunt-jitter 4
```

At the defaults, a 10,000-product run is intentionally long. The persistent state is designed for pausing overnight and resuming later.

## Failure handling

Each item records its current stage, failure count, last error, and completed payloads. A single scrape or Gemini failure does not discard other progress. Publish-batch failures stop the run because they usually indicate a deployment, token, D1, or Vectorize problem that should be fixed before sending more batches.

Run with `--verbose` before the command for more diagnostics:

```bash
ph-magic-import --verbose run --limit 3
```

## Tests

```bash
python -m unittest discover -s tests -v
```

The importer uses only Python's standard library, so there are no runtime packages beyond the editable install itself.
