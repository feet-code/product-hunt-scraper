from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from typing import Callable, Iterable
from urllib.parse import quote

from .http import HttpError, PoliteHttpClient
from .models import CatalogDraft, ExternalPage, SourceProduct, TransformedProduct


LOGGER = logging.getLogger(__name__)
REQUIRED_FIELDS = {
    "name",
    "category",
    "audience",
    "problem",
    "promise",
    "differentiator",
    "workflow",
    "keywords",
    "metrics",
}
GENERIC_NAME_TOKENS = {
    "app",
    "apps",
    "beta",
    "cloud",
    "flow",
    "hub",
    "labs",
    "platform",
    "software",
    "studio",
    "suite",
    "sync",
    "tool",
    "tools",
    "work",
}
WORD_PATTERN = re.compile(r"[a-z0-9]+")


PRODUCT_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string", "description": "A distinct 3-64 character name."},
        "category": {"type": "string", "description": "A 3-48 character category."},
        "audience": {"type": "string", "description": "Specific intended users."},
        "problem": {"type": "string", "description": "The operational problem."},
        "promise": {"type": "string", "description": "A realistic value proposition."},
        "differentiator": {
            "type": "string",
            "description": "A concrete, supportable differentiator.",
        },
        "workflow": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {"type": "string"},
        },
        "keywords": {
            "type": "array",
            "minItems": 4,
            "maxItems": 8,
            "items": {"type": "string"},
        },
        "metrics": {
            "type": "array",
            "minItems": 2,
            "maxItems": 4,
            "items": {"type": "string"},
        },
    },
    "required": sorted(REQUIRED_FIELDS),
}


class TransformationError(RuntimeError):
    def __init__(self, attempts: list[dict[str, str]]) -> None:
        super().__init__(
            "Every configured Gemini model failed: "
            + "; ".join(
                f"{item['model']}: {item['error']}" for item in attempts
            )
        )
        self.attempts = attempts


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.lower())
    without_marks = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return " ".join(WORD_PATTERN.findall(without_marks))


def public_draft_text(draft: CatalogDraft) -> str:
    return " ".join(
        [
            draft.name,
            draft.category,
            draft.audience,
            draft.problem,
            draft.promise,
            draft.differentiator,
            *draft.workflow,
            *draft.keywords,
            *draft.metrics,
        ]
    )


def source_text(source: SourceProduct, external_page: ExternalPage | None) -> str:
    parts = [source.tagline, source.description]
    if external_page and not external_page.skipped_reason:
        parts.extend(
            [external_page.title, external_page.description, external_page.text]
        )
    return " ".join(part for part in parts if part)


def source_content_hash(
    source: SourceProduct, external_page: ExternalPage | None
) -> str:
    canonical = json.dumps(
        {
            "source": source.to_dict(),
            "external_page": external_page.to_dict() if external_page else None,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _word_ngrams(value: str, size: int) -> set[tuple[str, ...]]:
    words = normalize_text(value).split()
    return {tuple(words[index : index + size]) for index in range(len(words) - size + 1)}


def _bigrams(value: str) -> set[str]:
    compact = normalize_text(value).replace(" ", "")
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[index : index + 2] for index in range(len(compact) - 1)}


def _dice_similarity(left: str, right: str) -> float:
    left_bigrams = _bigrams(left)
    right_bigrams = _bigrams(right)
    if not left_bigrams or not right_bigrams:
        return 0.0
    return (
        2 * len(left_bigrams.intersection(right_bigrams))
    ) / (len(left_bigrams) + len(right_bigrams))


def transformation_issues(
    source: SourceProduct,
    external_page: ExternalPage | None,
    draft: CatalogDraft,
) -> list[str]:
    issues: list[str] = []
    normalized_source_name = normalize_text(source.source_name)
    normalized_public = normalize_text(public_draft_text(draft))
    public_tokens = set(normalized_public.split())
    if (
        len(normalized_source_name) >= 3
        and f" {normalized_source_name} " in f" {normalized_public} "
    ):
        issues.append("source brand appears in public copy")
    for token in normalized_source_name.split():
        if (
            len(token) >= 4
            and token not in GENERIC_NAME_TOKENS
            and token in public_tokens
        ):
            issues.append(f"source brand token appears in public copy: {token}")
    if _dice_similarity(source.source_name, draft.name) >= 0.62:
        issues.append("replacement name is too similar to source name")

    original_ngrams = _word_ngrams(source_text(source, external_page), 7)
    draft_ngrams = _word_ngrams(public_draft_text(draft), 7)
    if original_ngrams.intersection(draft_ngrams):
        issues.append("public copy contains a seven-word source phrase")
    return list(dict.fromkeys(issues))


def _required_string(value: object, field: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    clean = re.sub(r"\s+", " ", value).strip()
    if not minimum <= len(clean) <= maximum:
        raise ValueError(f"{field} must contain {minimum}-{maximum} characters")
    return clean


def _required_string_list(
    value: object,
    field: str,
    minimum_items: int,
    maximum_items: int,
    minimum_length: int,
    maximum_length: int,
) -> list[str]:
    if not isinstance(value, list) or not minimum_items <= len(value) <= maximum_items:
        raise ValueError(
            f"{field} must contain {minimum_items}-{maximum_items} strings"
        )
    return [
        _required_string(item, f"{field}[{index}]", minimum_length, maximum_length)
        for index, item in enumerate(value)
    ]


def validate_draft(value: object) -> CatalogDraft:
    if not isinstance(value, dict) or set(value) != REQUIRED_FIELDS:
        raise ValueError("Gemini returned missing, extra, or non-object product fields")
    return CatalogDraft(
        name=_required_string(value["name"], "name", 3, 64),
        category=_required_string(value["category"], "category", 3, 48),
        audience=_required_string(value["audience"], "audience", 8, 160),
        problem=_required_string(value["problem"], "problem", 20, 360),
        promise=_required_string(value["promise"], "promise", 12, 220),
        differentiator=_required_string(
            value["differentiator"], "differentiator", 20, 420
        ),
        workflow=_required_string_list(value["workflow"], "workflow", 3, 4, 8, 180),
        keywords=_required_string_list(value["keywords"], "keywords", 4, 10, 2, 60),
        metrics=_required_string_list(value["metrics"], "metrics", 2, 4, 3, 100),
    )


def _response_text(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ValueError("Gemini returned a non-object response")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        feedback = payload.get("promptFeedback")
        raise ValueError(f"Gemini returned no candidates: {feedback}")
    content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise ValueError("Gemini returned no content parts")
    text = "".join(
        str(part.get("text") or "") for part in parts if isinstance(part, dict)
    ).strip()
    if not text:
        raise ValueError("Gemini returned an empty product")
    return text


def _model_payload(
    source: SourceProduct,
    external_page: ExternalPage | None,
    previous_failures: Iterable[str],
) -> dict:
    external_signals = None
    if external_page:
        external_signals = {
            "url": external_page.final_url,
            "title": external_page.title[:300],
            "description": external_page.description[:1_500],
            "page_text": external_page.text[:6_000],
            "skipped_reason": external_page.skipped_reason,
        }
    facts = {
        "source_brand_to_never_repeat": source.source_name,
        "source_brand_tokens_to_avoid": [
            token
            for token in normalize_text(source.source_name).split()
            if len(token) >= 4 and token not in GENERIC_NAME_TOKENS
        ],
        "product_hunt_signals": {
            "tagline": source.tagline[:800],
            "description": source.description[:4_000],
            "categories": source.categories[:12],
        },
        "external_website_signals": external_signals,
        "previous_attempt_problems": list(previous_failures)[-6:],
    }
    system = " ".join(
        [
            "Create one independently written Magic Catalog product record from general factual problem, audience, and workflow signals in the supplied research.",
            "Invent a completely different product name and never output the source brand, its distinctive tokens, maker names, URLs, slogans, or trademarked phrasing.",
            "Do not lightly paraphrase or remix source sentences; reason from the underlying user need and write fresh copy with a different structure and vocabulary.",
            "Do not claim integrations, performance, customers, certifications, pricing, or capabilities that the research does not support.",
            "Use present tense, make each field useful on its own, and return only the requested JSON object.",
        ]
    )
    return {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": "Research signals (not copy to reproduce):\n"
                        + json.dumps(facts, ensure_ascii=False, sort_keys=True)
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.45,
            "maxOutputTokens": 2_048,
            "responseMimeType": "application/json",
            "responseJsonSchema": PRODUCT_JSON_SCHEMA,
        },
    }


class GeminiTransformer:
    def __init__(
        self,
        *,
        client: PoliteHttpClient,
        api_key: str,
        models: tuple[str, ...],
    ) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required before transforming products.")
        self.client = client
        self.api_key = api_key
        self.models = models

    def transform(
        self,
        source: SourceProduct,
        external_page: ExternalPage | None,
        name_exists: Callable[[str], bool],
    ) -> TransformedProduct:
        attempts: list[dict[str, str]] = []
        previous_failures: list[str] = []
        for model in self.models:
            endpoint = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                + quote(model, safe="")
                + ":generateContent"
            )
            try:
                response = self.client.post_json(
                    endpoint,
                    _model_payload(source, external_page, previous_failures),
                    max_bytes=1_000_000,
                    headers={"x-goog-api-key": self.api_key},
                )
                payload = json.loads(response.text())
                draft = validate_draft(json.loads(_response_text(payload)))
                issues = transformation_issues(source, external_page, draft)
                if name_exists(draft.name):
                    issues.append("replacement name already exists in this catalog run")
                if issues:
                    raise ValueError("; ".join(issues))
                LOGGER.info("Transformed %s with %s", source.external_id, model)
                return TransformedProduct(
                    source=source,
                    external_page=external_page,
                    draft=draft,
                    generation_model=model,
                    source_content_hash=source_content_hash(source, external_page),
                )
            except (HttpError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                message = str(error)[:500]
                if isinstance(error, HttpError) and error.body:
                    message += ": " + re.sub(r"\s+", " ", error.body)[:300]
                attempts.append({"model": model, "error": message})
                previous_failures.append(message)
                LOGGER.warning("Gemini %s failed for %s: %s", model, source.external_id, message)
        raise TransformationError(attempts)
