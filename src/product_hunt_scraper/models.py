from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class SitemapEntry:
    url: str
    last_modified: str | None = None


@dataclass(frozen=True)
class SourceProduct:
    external_id: str
    source_url: str
    source_name: str
    tagline: str = ""
    description: str = ""
    website_url: str | None = None
    categories: list[str] = field(default_factory=list)
    last_modified: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SourceProduct:
        return cls(
            external_id=str(value["external_id"]),
            source_url=str(value["source_url"]),
            source_name=str(value["source_name"]),
            tagline=str(value.get("tagline") or ""),
            description=str(value.get("description") or ""),
            website_url=(str(value["website_url"]) if value.get("website_url") else None),
            categories=[str(item) for item in value.get("categories", [])],
            last_modified=(
                str(value["last_modified"]) if value.get("last_modified") else None
            ),
        )


@dataclass(frozen=True)
class ExternalPage:
    requested_url: str
    final_url: str
    title: str = ""
    description: str = ""
    text: str = ""
    skipped_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ExternalPage:
        return cls(
            requested_url=str(value["requested_url"]),
            final_url=str(value["final_url"]),
            title=str(value.get("title") or ""),
            description=str(value.get("description") or ""),
            text=str(value.get("text") or ""),
            skipped_reason=(
                str(value["skipped_reason"]) if value.get("skipped_reason") else None
            ),
        )


@dataclass(frozen=True)
class CatalogDraft:
    name: str
    category: str
    audience: str
    problem: str
    promise: str
    differentiator: str
    workflow: list[str]
    keywords: list[str]
    metrics: list[str]
    intentKey: str = "general-software-utilities"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CatalogDraft:
        return cls(
            intentKey=str(value.get("intentKey") or "general-software-utilities"),
            name=str(value["name"]),
            category=str(value["category"]),
            audience=str(value["audience"]),
            problem=str(value["problem"]),
            promise=str(value["promise"]),
            differentiator=str(value["differentiator"]),
            workflow=[str(item) for item in value["workflow"]],
            keywords=[str(item) for item in value["keywords"]],
            metrics=[str(item) for item in value["metrics"]],
        )


@dataclass(frozen=True)
class TransformedProduct:
    source: SourceProduct
    external_page: ExternalPage | None
    draft: CatalogDraft
    generation_model: str
    source_content_hash: str

    def import_record(self) -> dict[str, Any]:
        return {
            "externalId": self.source.external_id,
            "sourceUrl": self.source.source_url,
            "sourceWebsiteUrl": (
                self.external_page.final_url
                if self.external_page and not self.external_page.skipped_reason
                else self.source.website_url
            ),
            "sourceName": self.source.source_name,
            "sourceContentHash": self.source_content_hash,
            "generationModel": self.generation_model,
            "product": self.draft.to_dict(),
        }

