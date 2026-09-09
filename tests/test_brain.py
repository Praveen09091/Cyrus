"""Brain layer tests.

The valuable assertions here are the refusals: a page cannot claim to be
sourced without a citation, a citation must point at a file that exists, and
raw sources are never overwritten.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cyrus.brain.wiki import Brain, WikiPage, parse_frontmatter, slugify


class BrainFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="cyrus_brain_")
        self.brain = Brain(self.root)
        self.brain.ensure()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class TestIngest(BrainFixture):
    def test_a_source_lands_with_provenance(self):
        rel = self.brain.ingest("FOMC minutes", "Rates held.", "https://example.com/fomc")
        with open(os.path.join(self.root, rel), "r", encoding="utf-8") as handle:
            text = handle.read()
        meta = parse_frontmatter(text)
        self.assertEqual(meta["source"], "https://example.com/fomc")
        self.assertIn("Rates held.", text)

    def test_a_second_ingest_never_overwrites_the_first(self):
        """A source is evidence. Corrections are new files, not rewrites."""
        first = self.brain.ingest("Same title", "original content", "src-a")
        second = self.brain.ingest("Same title", "revised content", "src-b")
        self.assertNotEqual(first, second)

        with open(os.path.join(self.root, first), "r", encoding="utf-8") as handle:
            self.assertIn("original content", handle.read())


class TestWikiPages(BrainFixture):
    def test_a_sourced_page_must_cite_something(self):
        page = WikiPage(
            slug="spy", page_type="entity", subject="SPY",
            body="Index ETF.", evidence="sourced", sources=[],
        )
        with self.assertRaises(ValueError):
            self.brain.write_page(page)

    def test_an_unknown_page_type_is_rejected(self):
        page = WikiPage(slug="x", page_type="hot-take", subject="X", body="...")
        with self.assertRaises(ValueError):
            self.brain.write_page(page)

    def test_an_assumption_page_needs_no_citation(self):
        page = WikiPage(
            slug="hunch", page_type="concept", subject="A hunch",
            body="Unverified.", evidence="assumption",
        )
        rel = self.brain.write_page(page)
        self.assertTrue(os.path.exists(os.path.join(self.root, rel)))


class TestLint(BrainFixture):
    def test_a_citation_to_a_missing_source_is_an_error(self):
        self.brain.write_page(
            WikiPage(
                slug="ghost", page_type="entity", subject="Ghost",
                body="Cites nothing real.", evidence="sourced",
                sources=["raw/2020-01-01--does-not-exist.md"],
            )
        )
        findings = self.brain.lint()
        self.assertTrue(
            any(f.severity == "error" and "missing source" in f.message for f in findings),
            [f.message for f in findings],
        )

    def test_a_valid_citation_passes(self):
        source = self.brain.ingest("Real source", "content", "https://example.com")
        self.brain.write_page(
            WikiPage(
                slug="grounded", page_type="entity", subject="Grounded",
                body="Backed by a real file.", evidence="sourced", sources=[source],
            )
        )
        findings = self.brain.lint()
        self.assertFalse(
            any("missing source" in f.message for f in findings),
            [f.message for f in findings],
        )

    def test_orphan_pages_are_flagged(self):
        self.brain.write_page(
            WikiPage(slug="lonely", page_type="concept", subject="Lonely", body="No inbound links.")
        )
        findings = self.brain.lint()
        self.assertTrue(any("orphan" in f.message for f in findings))

    def test_a_link_to_a_nonexistent_page_is_flagged(self):
        self.brain.write_page(
            WikiPage(
                slug="linker", page_type="concept", subject="Linker",
                body="See [[a-page-that-does-not-exist]].",
            )
        )
        findings = self.brain.lint()
        self.assertTrue(any("does not exist" in f.message for f in findings))


class TestIndexAndLog(BrainFixture):
    def test_index_lists_written_pages(self):
        self.brain.write_page(
            WikiPage(slug="spy", page_type="entity", subject="SPY", body="Index ETF.")
        )
        rel = self.brain.rebuild_index()
        with open(os.path.join(self.root, rel), "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("SPY", text)
        self.assertIn("entity", text)

    def test_index_is_honest_when_empty(self):
        rel = self.brain.rebuild_index()
        with open(os.path.join(self.root, rel), "r", encoding="utf-8") as handle:
            self.assertIn("No pages yet", handle.read())

    def test_log_appends_rather_than_replaces(self):
        self.brain.log_event("first lesson")
        self.brain.log_event("second lesson")
        with open(os.path.join(self.brain.wiki_dir, "log.md"), "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("first lesson", text)
        self.assertIn("second lesson", text)


class TestSlugify(unittest.TestCase):
    def test_slugs_are_filesystem_safe(self):
        self.assertEqual(slugify("FOMC: Rates Held! (Sept)"), "fomc-rates-held-sept")

    def test_empty_input_gets_a_name(self):
        self.assertEqual(slugify("   "), "untitled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
