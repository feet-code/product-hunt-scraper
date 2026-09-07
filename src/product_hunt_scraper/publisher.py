from __future__ import annotations

import json
from dataclasses import dataclass

from .http import HttpError, PoliteHttpClient


@dataclass(frozen=True)
class PublishedProduct:
    external_id: str
    slug: str
    name: str
    created: bool


class MagicCatalogPublisher:
    def __init__(
        self,
        *,
        client: PoliteHttpClient,
        site_url: str,
        import_token: str,
    ) -> None:
        if not site_url:
            raise ValueError("MAGIC_CATALOG_URL is required with --publish.")
        if not import_token:
            raise ValueError("MAGIC_CATALOG_IMPORT_TOKEN is required with --publish.")
        self.client = client
        self.endpoint = site_url.rstrip("/") + "/api/admin/import-products"
        self.import_token = import_token

    def publish(self, records: list[dict]) -> list[PublishedProduct]:
        if not 1 <= len(records) <= 20:
            raise ValueError("Magic Catalog import batches must contain 1-20 products.")
        try:
            response = self.client.post_json(
                self.endpoint,
                {"products": records},
                max_bytes=2_000_000,
                headers={"Authorization": "Bearer " + self.import_token},
            )
        except HttpError as error:
            detail = error.body or str(error)
            raise RuntimeError(
                f"Magic Catalog import returned HTTP {error.status}: {detail[:1_000]}"
            ) from error

        try:
            payload = json.loads(response.text())
            products = payload["products"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise RuntimeError("Magic Catalog returned an invalid import response.") from error
        if not isinstance(products, list) or len(products) != len(records):
            raise RuntimeError("Magic Catalog returned the wrong number of imported products.")

        published: list[PublishedProduct] = []
        for record, item in zip(records, products, strict=True):
            if not isinstance(item, dict) or not item.get("slug") or not item.get("name"):
                raise RuntimeError("Magic Catalog returned an incomplete product result.")
            published.append(
                PublishedProduct(
                    external_id=str(record["externalId"]),
                    slug=str(item["slug"]),
                    name=str(item["name"]),
                    created=bool(item.get("created")),
                )
            )
        return published

