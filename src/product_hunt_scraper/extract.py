from __future__ import annotations

import gzip
import io
import json
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Any, Iterable, Iterator

from .http import HttpError, PoliteHttpClient, RobotsPolicy
from .models import ExternalPage, SitemapEntry, SourceProduct


LOGGER = logging.getLogger(__name__)
PRODUCT_PATH = re.compile(r"^/products/([a-z0-9]+(?:-[a-z0-9]+)*)/?$")
TRACKING_QUERY_KEYS = {
    "ref",
    "referrer",
    "source",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}


def collapse_text(value: str, limit: int | None = None) -> str:
    clean = re.sub(r"\s+", " ", value).strip()
    if limit is not None:
        clean = clean[:limit].rstrip()
    return clean


def clean_tracking_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    filtered = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS and not key.lower().startswith("utm_")
    ]
    return urllib.parse.urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            urllib.parse.urlencode(filtered, doseq=True),
            "",
        )
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_product_sitemap(data: bytes) -> list[SitemapEntry]:
    if data[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as archive:
            data = archive.read(256_000_001)
        if len(data) > 256_000_000:
            raise ValueError("The decompressed sitemap exceeded 256 MB.")
    entries: list[SitemapEntry] = []
    for _event, element in ET.iterparse(io.BytesIO(data), events=("end",)):
        if _local_name(element.tag) != "url":
            continue
        location = None
        last_modified = None
        for child in element:
            name = _local_name(child.tag)
            if name == "loc":
                location = collapse_text(child.text or "")
            elif name == "lastmod":
                last_modified = collapse_text(child.text or "") or None
        if location:
            parsed = urllib.parse.urlsplit(location)
            if parsed.hostname in {"producthunt.com", "www.producthunt.com"}:
                match = PRODUCT_PATH.fullmatch(parsed.path)
                if match:
                    entries.append(
                        SitemapEntry(
                            url=f"https://www.producthunt.com/products/{match.group(1)}",
                            last_modified=last_modified,
                        )
                    )
        element.clear()
    return entries


class SignalHtmlParser(HTMLParser):
    ignored_tags = {"footer", "form", "header", "nav", "noscript", "style", "svg"}
    block_tags = {
        "article",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "li",
        "main",
        "p",
        "section",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.text_parts: list[str] = []
        self.json_ld_blocks: list[str] = []
        self.meta_description = ""
        self.og_description = ""
        self.og_title = ""
        self.website_url: str | None = None
        self._in_title = False
        self._in_h1 = False
        self._json_ld_parts: list[str] | None = None
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = {key.lower(): value or "" for key, value in attrs}
        if self._ignored_depth:
            self._ignored_depth += 1
            return
        if tag in self.ignored_tags:
            self._ignored_depth = 1
            return
        if tag == "script":
            if attributes.get("type", "").lower() == "application/ld+json":
                self._json_ld_parts = []
            else:
                self._ignored_depth = 1
            return
        if tag == "title":
            self._in_title = True
        elif tag == "h1":
            self._in_h1 = True
        elif tag == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            content = attributes.get("content", "")
            if key == "description" and not self.meta_description:
                self.meta_description = content
            elif key == "og:description" and not self.og_description:
                self.og_description = content
            elif key == "og:title" and not self.og_title:
                self.og_title = content
        elif tag == "a" and attributes.get("data-test") == "visit-website-button":
            href = attributes.get("href", "")
            if href:
                self.website_url = href
        if tag in self.block_tags:
            self.text_parts.append("\n")

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if not self._ignored_depth:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._ignored_depth:
            self._ignored_depth -= 1
            return
        if tag == "script" and self._json_ld_parts is not None:
            self.json_ld_blocks.append("".join(self._json_ld_parts))
            self._json_ld_parts = None
            return
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
        if tag in self.block_tags:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._json_ld_parts is not None:
            self._json_ld_parts.append(data)
            return
        if self._ignored_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        if self._in_h1:
            self.h1_parts.append(data)
        if data.strip():
            self.text_parts.append(data)

    @property
    def title(self) -> str:
        return collapse_text(" ".join(self.title_parts))

    @property
    def h1(self) -> str:
        return collapse_text(" ".join(self.h1_parts))

    @property
    def visible_text(self) -> str:
        return collapse_text(" ".join(self.text_parts))


def _json_objects(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _json_objects(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _json_objects(nested)


def _json_ld_objects(parser: SignalHtmlParser) -> Iterator[dict[str, Any]]:
    for raw in parser.json_ld_blocks:
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        yield from _json_objects(parsed)


def _schema_types(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value.lower()}
    if isinstance(value, list):
        return {str(item).lower() for item in value}
    return set()


def _best_product_schema(parser: SignalHtmlParser, source_url: str) -> dict[str, Any]:
    best: dict[str, Any] = {}
    best_score = -1
    source_path = urllib.parse.urlsplit(source_url).path.rstrip("/")
    for candidate in _json_ld_objects(parser):
        types = _schema_types(candidate.get("@type"))
        if not types.intersection({"product", "softwareapplication", "webapplication"}):
            continue
        score = 0
        if candidate.get("name"):
            score += 3
        if candidate.get("description"):
            score += 4
        identifier = str(candidate.get("@id") or candidate.get("url") or "")
        if source_path and source_path in identifier:
            score += 3
        if candidate.get("applicationCategory") or candidate.get("category"):
            score += 1
        if score > best_score:
            best = candidate
            best_score = score
    return best


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [collapse_text(item) for item in value.split(",") if collapse_text(item)]
    if isinstance(value, list):
        return [collapse_text(str(item)) for item in value if collapse_text(str(item))]
    return []


def _title_without_product_hunt_suffix(value: str) -> str:
    return collapse_text(
        re.sub(r"\s*[-|:]\s*Product Hunt.*$", "", value, flags=re.IGNORECASE)
    )


def parse_product_hunt_page(
    html: str,
    *,
    source_url: str,
    last_modified: str | None = None,
) -> SourceProduct:
    parsed_url = urllib.parse.urlsplit(source_url)
    match = PRODUCT_PATH.fullmatch(parsed_url.path)
    if not match:
        raise ValueError(f"Not a Product Hunt product URL: {source_url}")

    parser = SignalHtmlParser()
    parser.feed(html)
    schema = _best_product_schema(parser, source_url)
    source_name = collapse_text(str(schema.get("name") or parser.h1 or parser.og_title))
    source_name = _title_without_product_hunt_suffix(source_name)
    if not source_name:
        raise ValueError("The Product Hunt page did not contain a product name.")

    description = collapse_text(str(schema.get("description") or ""), 4_000)
    tagline = collapse_text(
        parser.meta_description or parser.og_description or description,
        800,
    )
    website_url = parser.website_url
    if website_url:
        website_url = urllib.parse.urljoin(source_url, website_url)
        website_url = clean_tracking_url(website_url)
    categories = _string_list(
        schema.get("applicationCategory") or schema.get("category") or []
    )[:12]

    return SourceProduct(
        external_id=match.group(1),
        source_url=f"https://www.producthunt.com/products/{match.group(1)}",
        source_name=source_name[:160],
        tagline=tagline,
        description=description,
        website_url=website_url,
        categories=categories,
        last_modified=last_modified,
    )


def extract_external_page(
    client: PoliteHttpClient,
    robots: RobotsPolicy,
    url: str,
) -> ExternalPage:
    requested_url = clean_tracking_url(url)
    try:
        allowed, reason = robots.can_fetch(requested_url)
    except (ValueError, OSError) as error:
        return ExternalPage(
            requested_url=requested_url,
            final_url=requested_url,
            skipped_reason="unsafe-or-unresolvable-url:" + collapse_text(str(error), 120),
        )
    if not allowed:
        return ExternalPage(
            requested_url=requested_url,
            final_url=requested_url,
            skipped_reason=reason or "robots-disallowed",
        )

    try:
        response = client.get(
            requested_url,
            max_bytes=3_000_000,
            validate_external_url=True,
        )
    except HttpError as error:
        return ExternalPage(
            requested_url=requested_url,
            final_url=requested_url,
            skipped_reason=f"http-error:{error.status or 'network'}",
        )
    if response.content_type not in {"text/html", "application/xhtml+xml", "text/plain", ""}:
        return ExternalPage(
            requested_url=requested_url,
            final_url=response.url,
            skipped_reason="unsupported-content-type:" + response.content_type,
        )

    if response.content_type == "text/plain":
        text = collapse_text(response.text(), 6_000)
        return ExternalPage(
            requested_url=requested_url,
            final_url=clean_tracking_url(response.url),
            text=text,
        )

    parser = SignalHtmlParser()
    parser.feed(response.text())
    schema = _best_product_schema(parser, response.url)
    title = collapse_text(
        str(schema.get("name") or parser.og_title or parser.title or parser.h1),
        300,
    )
    description = collapse_text(
        str(
            schema.get("description")
            or parser.meta_description
            or parser.og_description
            or ""
        ),
        1_500,
    )
    visible_text = collapse_text(parser.visible_text, 6_000)
    return ExternalPage(
        requested_url=requested_url,
        final_url=clean_tracking_url(response.url),
        title=title,
        description=description,
        text=visible_text,
    )


def source_copy_parts(
    source: SourceProduct, external_page: ExternalPage | None
) -> Iterable[str]:
    yield source.tagline
    yield source.description
    if external_page and not external_page.skipped_reason:
        yield external_page.title
        yield external_page.description
        yield external_page.text
