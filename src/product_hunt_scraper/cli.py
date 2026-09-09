from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import DEFAULT_PRODUCT_SITEMAP, Settings, load_dotenv
from .gemini import GeminiTransformer
from .http import PoliteHttpClient, RobotsPolicy
from .pipeline import PipelineStats, ProductHuntPipeline, write_preview
from .publisher import MagicCatalogPublisher
from .state import StateStore
from .bulk import run_bulk, saved_entries
from .batch import Ledger


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ph-magic-import",
        description="Scrape Product Hunt slowly, synthesize original products, and import them into Magic Catalog.",
    )
    parser.add_argument(
        "--env-file",
        type=_path,
        default=_path(".env"),
        help="dotenv file to load (default: .env)",
    )
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="discover, scrape, transform, and optionally publish")
    run.add_argument(
        "--limit",
        type=int,
        default=3,
        help="target at most this many Product Hunt products (default: 3; no fixed maximum)",
    )
    run.add_argument("--input", type=_path, help="Publish edited JSON/JSONL exports using their existing slugs; only with --stage publish")
    run.add_argument("--offset", type=int, default=0, help="skip this many sitemap entries")
    run.add_argument(
        "--order", choices=("newest", "sitemap"), default="newest"
    )
    run.add_argument(
        "--publish",
        action="store_true",
        help="publish after generation (works with --stage generate or all)",
    )
    run.add_argument(
        "--no-external",
        action="store_true",
        help="do not fetch the external website linked from each Product Hunt page",
    )
    run.add_argument("--state", type=_path, default=_path(".state/scraper.sqlite3"))
    run.add_argument(
        "--preview", type=_path, default=_path(".state/products.jsonl")
    )
    run.add_argument("--sitemap-url", default=DEFAULT_PRODUCT_SITEMAP)
    run.add_argument(
        "--product-hunt-delay",
        type=float,
        default=5.0,
        help="minimum seconds between Product Hunt requests (minimum enforced: 2)",
    )
    run.add_argument("--product-hunt-jitter", type=float, default=2.0)
    run.add_argument("--external-delay", type=float, default=2.0)
    run.add_argument("--external-jitter", type=float, default=1.0)
    run.add_argument("--timeout", type=float, default=30.0)
    run.add_argument("--gemini-timeout", type=float, default=180.0, help="Gemini response timeout in seconds (default: 180)")
    run.add_argument("--http-attempts", type=int, default=5)
    run.add_argument("--max-failures", type=int, default=3)
    run.add_argument("--publish-batch-size", type=int, default=25)
    run.add_argument("--stage",choices=['all','scrape','generate','generate-publish','publish'],default='all')
    run.add_argument("--scrape-only",action='store_true')
    run.add_argument("--offline",action='store_true')
    run.add_argument("--batch-size",type=int,default=50,help="Fixed products per Gemini call; final partial batch may be smaller")
    run.add_argument("--max-batch-size",type=int,default=100,help=argparse.SUPPRESS)
    run.add_argument("--daily-row-budget",type=int,default=80000)
    run.add_argument("--wait-minutes",type=float,default=2)
    subparsers.add_parser('quota-status')
    audit_parser = subparsers.add_parser('audit-brands', help='Find known brand collisions in saved and published drafts')
    audit_parser.add_argument('--state', type=_path, default=_path('.state/scraper.sqlite3'))
    audit_parser.add_argument('--repair', action='store_true', help='Back up checkpoint and queue flagged records for regeneration at their existing URLs')

    status = subparsers.add_parser("status", help="show persistent checkpoint counts")
    status.add_argument("--state", type=_path, default=_path(".state/scraper.sqlite3"))

    export = subparsers.add_parser("export", help="export transformed products as JSONL")
    export.add_argument("--state", type=_path, default=_path(".state/scraper.sqlite3"))
    export.add_argument("--output", type=_path, default=_path(".state/products.jsonl"))
    export.add_argument("--limit", type=int)

    retry = subparsers.add_parser(
        "retry-failed", help="reset failure counters while preserving completed stages"
    )
    retry.add_argument("--state", type=_path, default=_path(".state/scraper.sqlite3"))
    return parser


def _client(settings: Settings, *, max_attempts: int) -> PoliteHttpClient:
    return PoliteHttpClient(
        user_agent=settings.user_agent,
        product_hunt_delay_seconds=settings.product_hunt_delay_seconds,
        product_hunt_jitter_seconds=settings.product_hunt_jitter_seconds,
        external_delay_seconds=settings.external_delay_seconds,
        external_jitter_seconds=settings.external_jitter_seconds,
        timeout_seconds=settings.request_timeout_seconds,
        max_attempts=max_attempts,
    )


def run_command(arguments: argparse.Namespace) -> int:
    if arguments.input and (arguments.stage != 'publish' or arguments.scrape_only):
        raise ValueError('--input requires --stage publish.')
    if arguments.input and arguments.offset:
        raise ValueError('--offset is not supported with --input.')
    if arguments.limit < 1:
        raise ValueError("--limit must be positive.")
    if arguments.offset < 0:
        raise ValueError("--offset must be zero or greater.")
    if not 1 <= arguments.publish_batch_size <= 25:
        raise ValueError("--publish-batch-size must be between 1 and 25.")

    settings = Settings.from_environment(
        state_path=arguments.state,
        preview_path=arguments.preview,
        sitemap_url=arguments.sitemap_url,
        product_hunt_delay_seconds=arguments.product_hunt_delay,
        product_hunt_jitter_seconds=arguments.product_hunt_jitter,
        external_delay_seconds=arguments.external_delay,
        external_jitter_seconds=arguments.external_jitter,
        request_timeout_seconds=arguments.timeout,
        max_http_attempts=arguments.http_attempts,
    )
    if arguments.scrape_only:
        if arguments.publish:
            raise ValueError('--scrape-only cannot be combined with --publish')
        arguments.stage = 'scrape'
    if arguments.stage == 'scrape' and arguments.publish:
        raise ValueError('--stage scrape cannot be combined with --publish; use --stage generate --publish afterward.')
    crawl_client = _client(settings,max_attempts=settings.max_http_attempts)
    gemini_client = _client(settings,max_attempts=1)
    if not 5 <= arguments.gemini_timeout <= 1800:
        raise ValueError('--gemini-timeout must be between 5 and 1800 seconds.')
    gemini_client.timeout_seconds = arguments.gemini_timeout
    publisher_client = _client(settings,max_attempts=1)
    stats = PipelineStats()
    with StateStore(settings.state_path) as state:
        pipeline = ProductHuntPipeline(
            state=state,
            crawl_client=crawl_client,
            robots=RobotsPolicy(crawl_client, settings.user_agent),
            transformer=None,
            publisher=None,
            sitemap_url=settings.sitemap_url,
            preview_path=settings.preview_path,
            follow_external=not arguments.no_external,
            publish_batch_size=arguments.publish_batch_size,
            max_failures=arguments.max_failures,
        )
        if arguments.input:
            from .file_edits import queue_file_edits
            entries, report = queue_file_edits(state, arguments.input)
            logging.getLogger(__name__).info('File edits: %s', json.dumps(report))
            entries = entries[:arguments.limit]
        elif arguments.stage in ('all','scrape') and not arguments.offline:
            entries = pipeline.discover(limit=arguments.limit,offset=arguments.offset,order=arguments.order,stats=stats)
        else:
            entries = saved_entries(state, arguments.limit, arguments.offset,
                stage=arguments.stage, publish=arguments.publish, max_failures=arguments.max_failures)
        return run_bulk(pipeline,entries,arguments,settings,gemini_client,publisher_client)


def status_command(arguments: argparse.Namespace) -> int:
    with StateStore(arguments.state) as state:
        result = state.status_counts()
    print(json.dumps({"state": str(arguments.state), "counts": result}, indent=2))
    return 0


def export_command(arguments: argparse.Namespace) -> int:
    if arguments.limit is not None and arguments.limit < 1:
        raise ValueError("--limit must be positive when provided.")
    with StateStore(arguments.state) as state:
        records = state.transformed_records(arguments.limit)
    write_preview(arguments.output, records)
    print(json.dumps({"exported": len(records), "output": str(arguments.output)}))
    return 0


def retry_command(arguments: argparse.Namespace) -> int:
    with StateStore(arguments.state) as state:
        reset = state.reset_failures()
    print(json.dumps({"reset": reset, "state": str(arguments.state)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    load_dotenv(arguments.env_file)
    logging.basicConfig(
        level=logging.DEBUG if arguments.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        if arguments.command == 'audit-brands':
            from .brand_audit import audit
            with StateStore(arguments.state) as state:
                result = audit(state, arguments.repair)
            print(json.dumps(result, indent=2))
            return 0
        if arguments.command == 'quota-status':
            from .config import configured_gemini_api_keys, configured_models
            keys = configured_gemini_api_keys()
            ledger = Ledger()
            pool = None
            try:
                if len(keys) > 1:
                    from .key_pool import GeminiKeyPoolLedger
                    pool = GeminiKeyPoolLedger(ledger, keys)
                    result = {
                        'mode': 'key-pool',
                        'configured_keys': len(keys),
                        'limits_per_key': {
                            'rpd': ledger.rpd,
                            'rpm': ledger.rpm,
                            'tpm': ledger.tpm,
                        },
                        'keys': pool.summary(configured_models()),
                    }
                else:
                    result = {
                        'mode': 'single-key',
                        'configured_keys': len(keys),
                        'models': ledger.summary(configured_models()),
                    }
                print(json.dumps(result,indent=2))
            finally:
                if pool is not None:
                    pool.close()
                ledger.db.close()
            return 0
        if arguments.command == "run":
            return run_command(arguments)
        if arguments.command == "status":
            return status_command(arguments)
        if arguments.command == "export":
            return export_command(arguments)
        if arguments.command == "retry-failed":
            return retry_command(arguments)
        parser.error("Unknown command.")
    except KeyboardInterrupt:
        logging.getLogger(__name__).warning(
            "Interrupted. Completed stages are checkpointed; rerun the same command to resume."
        )
        return 130
    except Exception as error:
        logging.getLogger(__name__).exception("%s", error)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
