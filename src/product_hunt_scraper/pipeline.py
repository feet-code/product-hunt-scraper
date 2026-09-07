from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .extract import (
    extract_external_page,
    parse_product_hunt_page,
    parse_product_sitemap,
)
from .gemini import GeminiTransformer, source_content_hash
from .http import PoliteHttpClient, RobotsPolicy
from .models import CatalogDraft, ExternalPage, SitemapEntry, SourceProduct
from .publisher import MagicCatalogPublisher
from .state import DuplicateDraftName, StateStore


LOGGER = logging.getLogger(__name__)


class PublishBatchError(RuntimeError):
    pass


def _last_modified_timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return 0.0


@dataclass
class PipelineStats:
    selected: int = 0
    newly_queued: int = 0
    already_published: int = 0
    scraped: int = 0
    external_scraped: int = 0
    external_skipped: int = 0
    transformed: int = 0
    published: int = 0
    created_in_catalog: int = 0
    already_in_catalog: int = 0
    failed: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def _import_record(
    source: SourceProduct,
    external_page: ExternalPage | None,
    draft: CatalogDraft,
    generation_model: str,
    content_hash: str,
) -> dict:
    return {
        "externalId": source.external_id,
        "sourceUrl": source.source_url,
        "sourceWebsiteUrl": (
            external_page.final_url
            if external_page and not external_page.skipped_reason
            else source.website_url
        ),
        "sourceName": source.source_name,
        "sourceContentHash": content_hash,
        "generationModel": generation_model,
        "product": draft.to_dict(),
    }


def write_preview(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    content = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        for record in records
    )
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


class ProductHuntPipeline:
    def __init__(
        self,
        *,
        state: StateStore,
        crawl_client: PoliteHttpClient,
        robots: RobotsPolicy,
        transformer: GeminiTransformer,
        publisher: MagicCatalogPublisher | None,
        sitemap_url: str,
        preview_path: Path,
        follow_external: bool,
        publish_batch_size: int,
        max_failures: int,
    ) -> None:
        self.state = state
        self.crawl_client = crawl_client
        self.robots = robots
        self.transformer = transformer
        self.publisher = publisher
        self.sitemap_url = sitemap_url
        self.preview_path = preview_path
        self.follow_external = follow_external
        self.publish_batch_size = max(1, min(publish_batch_size, 20))
        self.max_failures = max(1, max_failures)

    def discover(
        self, *, limit: int, offset: int, order: str, stats: PipelineStats
    ) -> list[SitemapEntry]:
        LOGGER.info("Fetching Product Hunt product sitemap: %s", self.sitemap_url)
        response = self.crawl_client.get(
            self.sitemap_url,
            max_bytes=128_000_000,
            accept="application/xml,text/xml,application/gzip,*/*;q=0.1",
        )
        entries = parse_product_sitemap(response.body)
        entries = list({entry.url: entry for entry in entries}.values())
        if order == "newest":
            entries.sort(
                key=lambda item: _last_modified_timestamp(item.last_modified),
                reverse=True,
            )
        selected = entries[offset : offset + limit]
        self.state.set_metadata("last_sitemap_url", self.sitemap_url)
        self.state.set_metadata("last_sitemap_product_count", str(len(entries)))
        stats.newly_queued = self.state.enqueue(selected)
        LOGGER.info(
            "Sitemap contains %d product pages; selected %d (offset %d)",
            len(entries),
            len(selected),
            offset,
        )
        return selected

    def _scrape_source(self, source_url: str, last_modified: str | None) -> SourceProduct:
        response = self.crawl_client.get(source_url, max_bytes=6_000_000)
        if response.content_type not in {"text/html", "application/xhtml+xml", ""}:
            raise ValueError(
                "Product Hunt returned unsupported content type: "
                + response.content_type
            )
        return parse_product_hunt_page(
            response.text(),
            source_url=source_url,
            last_modified=last_modified,
        )

    def _external_page(self, source: SourceProduct) -> ExternalPage | None:
        if not self.follow_external or not source.website_url:
            return None
        return extract_external_page(
            self.crawl_client,
            self.robots,
            source.website_url,
        )

    def _flush_publish_batch(
        self,
        pending: list[tuple[str, dict]],
        stats: PipelineStats,
    ) -> None:
        if not pending or not self.publisher:
            return
        records = [record for _source_url, record in pending]
        try:
            results = self.publisher.publish(records)
        except Exception as error:
            for source_url, _record in pending:
                self.state.mark_failed(source_url, "publish", error)
                stats.failed += 1
            pending.clear()
            raise PublishBatchError(str(error)) from error
        for (source_url, _record), result in zip(pending, results, strict=True):
            self.state.mark_published(source_url, result.slug)
            stats.published += 1
            if result.created:
                stats.created_in_catalog += 1
            else:
                stats.already_in_catalog += 1
            LOGGER.info(
                "Published %s as /product/%s (%s)",
                result.external_id,
                result.slug,
                "created" if result.created else "already present",
            )
        pending.clear()

    def run(
        self, entries: list[SitemapEntry], *, stats: PipelineStats
    ) -> PipelineStats:
        stats.selected = len(entries)
        pending_publish: list[tuple[str, dict]] = []
        for position, entry in enumerate(entries, start=1):
            item = self.state.get_work_item(entry.url)
            if item is None:
                LOGGER.error("State row disappeared for %s", entry.url)
                stats.failed += 1
                continue
            if item.status == "published":
                stats.already_published += 1
                continue
            if item.attempts >= self.max_failures:
                LOGGER.warning(
                    "Skipping %s after %d failures", entry.url, item.attempts
                )
                stats.failed += 1
                continue

            LOGGER.info("[%d/%d] Processing %s", position, len(entries), entry.url)
            source = item.source
            external_page = item.external_page
            draft = item.draft
            generation_model = item.generation_model
            content_hash = item.source_content_hash
            stage = "scrape-product-hunt"
            try:
                if source is None:
                    source = self._scrape_source(entry.url, entry.last_modified)
                    self.state.mark_scraped(entry.url, source)
                    stats.scraped += 1
                    LOGGER.info(
                        "Scraped %s (%s)", source.external_id, source.source_name
                    )

                stage = "scrape-external"
                if not item.external_checked:
                    external_page = self._external_page(source)
                    self.state.mark_external_checked(entry.url, external_page)
                    if external_page and external_page.skipped_reason:
                        stats.external_skipped += 1
                        LOGGER.info(
                            "Skipped external page for %s: %s",
                            source.external_id,
                            external_page.skipped_reason,
                        )
                    elif external_page:
                        stats.external_scraped += 1

                stage = "transform"
                if draft is None:
                    transformed = self.transformer.transform(
                        source,
                        external_page,
                        lambda name: self.state.draft_name_exists(name, entry.url),
                    )
                    draft = transformed.draft
                    generation_model = transformed.generation_model
                    content_hash = transformed.source_content_hash
                    self.state.mark_transformed(
                        entry.url,
                        draft,
                        generation_model,
                        content_hash,
                    )
                    stats.transformed += 1

                if not generation_model or not content_hash:
                    raise RuntimeError("Transformed state is missing model or content hash.")
                record = _import_record(
                    source,
                    external_page,
                    draft,
                    generation_model,
                    content_hash,
                )
                if self.publisher:
                    pending_publish.append((entry.url, record))
                    if len(pending_publish) >= self.publish_batch_size:
                        stage = "publish"
                        self._flush_publish_batch(pending_publish, stats)
            except KeyboardInterrupt:
                raise
            except PublishBatchError:
                raise
            except DuplicateDraftName as error:
                self.state.mark_failed(entry.url, stage, error)
                stats.failed += 1
                LOGGER.exception("Duplicate generated name for %s", entry.url)
            except Exception as error:
                self.state.mark_failed(entry.url, stage, error)
                stats.failed += 1
                LOGGER.exception("Failed at %s for %s", stage, entry.url)

        if pending_publish:
            self._flush_publish_batch(pending_publish, stats)

        write_preview(self.preview_path, self.state.transformed_records())
        LOGGER.info("Preview JSONL written to %s", self.preview_path)
        return stats
