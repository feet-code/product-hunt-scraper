from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import CatalogDraft, ExternalPage, SitemapEntry, SourceProduct


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


class DuplicateDraftName(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkItem:
    source_url: str
    external_id: str
    last_modified: str | None
    status: str
    source: SourceProduct | None
    external_page: ExternalPage | None
    external_checked: bool
    draft: CatalogDraft | None
    generation_model: str | None
    source_content_hash: str | None
    attempts: int
    failed_stage: str | None
    error: str | None


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self._initialize()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS work_items (
                source_url TEXT PRIMARY KEY,
                external_id TEXT NOT NULL,
                last_modified TEXT,
                ordinal INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                source_json TEXT,
                external_json TEXT,
                external_checked INTEGER NOT NULL DEFAULT 0,
                draft_json TEXT,
                draft_name_normalized TEXT,
                generation_model TEXT,
                source_content_hash TEXT,
                published_slug TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                failed_stage TEXT,
                error TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_work_items_external_id
                ON work_items(external_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_work_items_draft_name
                ON work_items(draft_name_normalized)
                WHERE draft_name_normalized IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_work_items_status_ordinal
                ON work_items(status, ordinal);
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        self.close()

    def set_metadata(self, key: str, value: str) -> None:
        self.connection.execute(
            """
            INSERT INTO metadata (key, value, updated_at) VALUES (?1, ?2, ?3)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, utc_now()),
        )
        self.connection.commit()

    def enqueue(self, entries: Iterable[SitemapEntry]) -> int:
        added = 0
        now = utc_now()
        with self.connection:
            for ordinal, entry in enumerate(entries):
                external_id = entry.url.rstrip("/").rsplit("/", 1)[-1]
                cursor = self.connection.execute(
                    """
                    INSERT INTO work_items (
                        source_url, external_id, last_modified, ordinal, updated_at
                    ) VALUES (?1, ?2, ?3, ?4, ?5)
                    ON CONFLICT(source_url) DO NOTHING
                    """,
                    (entry.url, external_id, entry.last_modified, ordinal, now),
                )
                was_added = cursor.rowcount == 1
                if was_added:
                    added += 1
                else:
                    self.connection.execute(
                        """
                        UPDATE work_items SET last_modified = ?1, ordinal = ?2
                        WHERE source_url = ?3
                        """,
                        (entry.last_modified, ordinal, entry.url),
                    )
        return added

    @staticmethod
    def _loads(value: str | None) -> dict | None:
        return json.loads(value) if value else None

    def _to_work_item(self, row: sqlite3.Row) -> WorkItem:
        source_value = self._loads(row["source_json"])
        external_value = self._loads(row["external_json"])
        draft_value = self._loads(row["draft_json"])
        return WorkItem(
            source_url=row["source_url"],
            external_id=row["external_id"],
            last_modified=row["last_modified"],
            status=row["status"],
            source=SourceProduct.from_dict(source_value) if source_value else None,
            external_page=(
                ExternalPage.from_dict(external_value) if external_value else None
            ),
            external_checked=bool(row["external_checked"]),
            draft=CatalogDraft.from_dict(draft_value) if draft_value else None,
            generation_model=row["generation_model"],
            source_content_hash=row["source_content_hash"],
            attempts=int(row["attempts"]),
            failed_stage=row["failed_stage"],
            error=row["error"],
        )

    def work_items(self, limit: int, max_failures: int) -> list[WorkItem]:
        rows = self.connection.execute(
            """
            SELECT * FROM work_items
            WHERE status != 'published' AND attempts < ?1
            ORDER BY ordinal ASC
            LIMIT ?2
            """,
            (max_failures, limit),
        ).fetchall()
        return [self._to_work_item(row) for row in rows]

    def get_work_item(self, source_url: str) -> WorkItem | None:
        row = self.connection.execute(
            "SELECT * FROM work_items WHERE source_url = ?1", (source_url,)
        ).fetchone()
        return self._to_work_item(row) if row else None

    def mark_scraped(self, source_url: str, source: SourceProduct) -> None:
        self.connection.execute(
            """
            UPDATE work_items SET
                status = 'scraped', source_json = ?1, error = NULL,
                failed_stage = NULL, updated_at = ?2
            WHERE source_url = ?3
            """,
            (json.dumps(source.to_dict(), sort_keys=True), utc_now(), source_url),
        )
        self.connection.commit()

    def mark_external_checked(
        self, source_url: str, external_page: ExternalPage | None
    ) -> None:
        payload = (
            json.dumps(external_page.to_dict(), sort_keys=True)
            if external_page is not None
            else None
        )
        self.connection.execute(
            """
            UPDATE work_items SET
                external_json = ?1, external_checked = 1,
                error = NULL, failed_stage = NULL, updated_at = ?2
            WHERE source_url = ?3
            """,
            (payload, utc_now(), source_url),
        )
        self.connection.commit()

    def draft_name_exists(self, name: str, excluding_source_url: str) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM work_items
            WHERE draft_name_normalized = ?1 AND source_url != ?2 LIMIT 1
            """,
            (normalized_name(name), excluding_source_url),
        ).fetchone()
        return row is not None

    def mark_transformed(
        self,
        source_url: str,
        draft: CatalogDraft,
        generation_model: str,
        source_content_hash: str,
    ) -> None:
        try:
            self.connection.execute(
                """
                UPDATE work_items SET
                    status = 'transformed', draft_json = ?1,
                    draft_name_normalized = ?2, generation_model = ?3,
                    source_content_hash = ?4, error = NULL,
                    failed_stage = NULL, updated_at = ?5
                WHERE source_url = ?6
                """,
                (
                    json.dumps(draft.to_dict(), sort_keys=True),
                    normalized_name(draft.name),
                    generation_model,
                    source_content_hash,
                    utc_now(),
                    source_url,
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as error:
            self.connection.rollback()
            raise DuplicateDraftName(
                f"Generated catalog name already exists: {draft.name}"
            ) from error

    def mark_published(self, source_url: str, slug: str) -> None:
        self.connection.execute(
            """
            UPDATE work_items SET
                status = 'published', published_slug = ?1,
                error = NULL, failed_stage = NULL, updated_at = ?2
            WHERE source_url = ?3
            """,
            (slug, utc_now(), source_url),
        )
        self.connection.commit()

    def mark_failed(self, source_url: str, stage: str, error: BaseException | str) -> None:
        message = str(error).strip() or error.__class__.__name__
        self.connection.execute(
            """
            UPDATE work_items SET
                status = 'failed', attempts = attempts + 1,
                failed_stage = ?1, error = ?2, updated_at = ?3
            WHERE source_url = ?4
            """,
            (stage, message[:2_000], utc_now(), source_url),
        )
        self.connection.commit()

    def reset_failures(self) -> int:
        cursor = self.connection.execute(
            """
            UPDATE work_items SET
                status = CASE
                    WHEN draft_json IS NOT NULL THEN 'transformed'
                    WHEN source_json IS NOT NULL THEN 'scraped'
                    ELSE 'queued'
                END,
                attempts = 0, failed_stage = NULL, error = NULL,
                updated_at = ?1
            WHERE status = 'failed'
            """,
            (utc_now(),),
        )
        self.connection.commit()
        return int(cursor.rowcount)

    def status_counts(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT status, count(*) AS count FROM work_items GROUP BY status"
        ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        counts["total"] = sum(counts.values())
        return counts

    def transformed_records(self, limit: int | None = None) -> list[dict]:
        query = (
            "SELECT source_json, external_json, draft_json, generation_model, "
            "source_content_hash FROM work_items WHERE draft_json IS NOT NULL "
            "ORDER BY ordinal"
        )
        parameters: tuple[int, ...] = ()
        if limit is not None:
            query += " LIMIT ?1"
            parameters = (limit,)
        rows = self.connection.execute(query, parameters).fetchall()
        records: list[dict] = []
        for row in rows:
            source = SourceProduct.from_dict(json.loads(row["source_json"]))
            external = (
                ExternalPage.from_dict(json.loads(row["external_json"]))
                if row["external_json"]
                else None
            )
            draft = CatalogDraft.from_dict(json.loads(row["draft_json"]))
            records.append(
                {
                    "externalId": source.external_id,
                    "sourceUrl": source.source_url,
                    "sourceWebsiteUrl": (
                        external.final_url
                        if external and not external.skipped_reason
                        else source.website_url
                    ),
                    "sourceName": source.source_name,
                    "sourceContentHash": row["source_content_hash"],
                    "generationModel": row["generation_model"],
                    "product": draft.to_dict(),
                }
            )
        return records
