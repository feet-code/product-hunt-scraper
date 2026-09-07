import tempfile
import unittest
from pathlib import Path

from product_hunt_scraper.models import CatalogDraft, SitemapEntry, SourceProduct
from product_hunt_scraper.state import DuplicateDraftName, StateStore


def draft(name):
    return CatalogDraft(
        name=name,
        category="Customer research",
        audience="product teams organizing customer interview evidence",
        problem="Interview notes become disconnected from decisions and follow-up work.",
        promise="Turn scattered evidence into a reviewable decision trail.",
        differentiator="Evidence and ownership stay together in one structured workflow.",
        workflow=["Import notes safely.", "Group repeated evidence.", "Assign follow-up work."],
        keywords=["research notes", "decision log", "evidence workflow", "follow up"],
        metrics=["open actions", "evidence coverage"],
    )


class StateTests(unittest.TestCase):
    def test_checkpoints_stages_and_enforces_unique_generated_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            entries = [
                SitemapEntry("https://www.producthunt.com/products/one"),
                SitemapEntry("https://www.producthunt.com/products/two"),
            ]
            with StateStore(path) as state:
                self.assertEqual(state.enqueue(entries), 2)
                self.assertEqual(state.enqueue(entries), 0)
                source = SourceProduct(
                    external_id="one",
                    source_url=entries[0].url,
                    source_name="Original One",
                )
                state.mark_scraped(entries[0].url, source)
                state.mark_external_checked(entries[0].url, None)
                state.mark_transformed(
                    entries[0].url, draft("Signal Grove"), "gemini-3.8-flash", "a" * 64
                )
                self.assertEqual(state.get_work_item(entries[0].url).status, "transformed")
                state.mark_transformed(
                    entries[1].url, draft("Another Name"), "gemini-3.8-flash", "b" * 64
                )
                with self.assertRaises(DuplicateDraftName):
                    state.mark_transformed(
                        entries[1].url,
                        draft("Signal Grove"),
                        "gemini-3.8-flash",
                        "b" * 64,
                    )
                state.mark_published(entries[0].url, "signal-grove-123")
                self.assertEqual(state.status_counts()["published"], 1)


if __name__ == "__main__":
    unittest.main()

