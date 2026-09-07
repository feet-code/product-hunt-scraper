import json
import unittest

from product_hunt_scraper.config import DEFAULT_GEMINI_MODELS
from product_hunt_scraper.gemini import GeminiTransformer, transformation_issues
from product_hunt_scraper.http import HttpError
from product_hunt_scraper.models import CatalogDraft, SourceProduct


def draft_value(name="Signal Grove"):
    return {
        "name": name,
        "category": "Customer research",
        "audience": "product teams organizing customer interview evidence",
        "problem": "Interview notes become disconnected from decisions, owners, and follow-up work.",
        "promise": "Turn scattered research evidence into a reviewable decision trail.",
        "differentiator": "Evidence, ownership, and follow-up decisions remain in one structured workflow.",
        "workflow": [
            "Import approved research notes and supporting context.",
            "Group recurring evidence around a specific decision.",
            "Assign the follow-up action and preserve its outcome.",
        ],
        "keywords": [
            "customer interview repository",
            "research decision log",
            "product evidence workflow",
            "user research follow up",
        ],
        "metrics": ["unassigned follow-up actions", "decision evidence coverage"],
    }


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def text(self):
        return json.dumps(self.payload)


class FakeClient:
    def __init__(self):
        self.urls = []

    def post_json(self, url, payload, **kwargs):
        self.urls.append(url)
        if len(self.urls) == 1:
            raise HttpError("rate limited", status=429)
        return FakeResponse(
            {
                "candidates": [
                    {"content": {"parts": [{"text": json.dumps(draft_value())}]}}
                ]
            }
        )


class GeminiTests(unittest.TestCase):
    def setUp(self):
        self.source = SourceProduct(
            external_id="acme-beacon",
            source_url="https://www.producthunt.com/products/acme-beacon",
            source_name="Acme Beacon",
            tagline="Keep customer notes connected to product decisions.",
            description="A repository that organizes interviews and follow-up work.",
        )

    def test_uses_requested_model_chain_in_order(self):
        client = FakeClient()
        transformer = GeminiTransformer(
            client=client,
            api_key="test-key",
            models=DEFAULT_GEMINI_MODELS,
        )
        result = transformer.transform(self.source, None, lambda _name: False)
        self.assertEqual(result.generation_model, "gemini-3.7-flash")
        self.assertIn("gemini-3.8-flash", client.urls[0])
        self.assertIn("gemini-3.7-flash", client.urls[1])

    def test_detects_source_brand_and_long_copied_phrase(self):
        clean = CatalogDraft.from_dict(draft_value())
        self.assertEqual(transformation_issues(self.source, None, clean), [])
        branded = CatalogDraft.from_dict(
            {
                **draft_value(),
                "differentiator": "Acme keeps evidence, ownership, and follow-up decisions in one workflow.",
            }
        )
        self.assertTrue(
            any("source brand token" in issue for issue in transformation_issues(self.source, None, branded))
        )
        copied = CatalogDraft.from_dict(
            {
                **draft_value(),
                "problem": "A repository that organizes interviews and follow-up work for product teams.",
            }
        )
        self.assertTrue(
            any("seven-word" in issue for issue in transformation_issues(self.source, None, copied))
        )


if __name__ == "__main__":
    unittest.main()

