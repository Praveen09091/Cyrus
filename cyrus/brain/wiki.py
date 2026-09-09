"""Brain layer: the llm-wiki pattern, adapted for a trading desk.

Three layers, after Karpathy's llm-wiki:
  raw/    immutable sources. Never edited after landing.
  wiki/   LLM-owned markdown. Entity pages, regimes, playbooks, post-mortems.
  ledger/ kernel-owned, append-only. Numbers live here, not in the wiki.

The important deviation from the original pattern: the wiki is rewritable and
the ledger is not. A model that can revise its own trade record cannot be
trusted about its own performance, so the ledger is off limits and the wiki
only ever *interprets* it.

This module provides the mechanics. It does not itself call a model: ingest
writes the source and the page skeleton, and an agent fills in the synthesis.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

VALID_TYPES = {"entity", "concept", "regime", "playbook", "postmortem", "summary"}
VALID_EVIDENCE = {"sourced", "mixed", "assumption"}


@dataclass
class WikiPage:
    slug: str
    page_type: str
    subject: str
    body: str
    evidence: str = "assumption"
    sources: List[str] = field(default_factory=list)
    updated: str = ""

    def render(self) -> str:
        updated = self.updated or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lines = [
            "---",
            "type: %s" % self.page_type,
            "subject: %s" % self.subject,
            "updated: %s" % updated,
            "evidence: %s" % self.evidence,
            "sources: [%s]" % ", ".join(self.sources),
            "---",
            "",
            self.body.strip(),
            "",
        ]
        return "\n".join(lines)


@dataclass
class LintFinding:
    severity: str  # error | warn
    path: str
    message: str


class Brain:
    """Filesystem operations for the three layers."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.raw_dir = os.path.join(root, "raw")
        self.wiki_dir = os.path.join(root, "wiki")
        self.ledger_dir = os.path.join(root, "ledger")

    def ensure(self) -> None:
        for path in (self.raw_dir, self.wiki_dir, self.ledger_dir):
            os.makedirs(path, exist_ok=True)
        for sub in ("entities", "regimes", "playbooks", "postmortems"):
            os.makedirs(os.path.join(self.wiki_dir, sub), exist_ok=True)

    # --- ingest -----------------------------------------------------------

    def ingest(self, title: str, content: str, source: str) -> str:
        """Land a source in raw/. Immutable once written.

        A second ingest of the same title on the same day gets a suffix rather
        than overwriting, because the original is evidence.
        """
        self.ensure()
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        base = "%s--%s" % (day, slugify(title))
        path = os.path.join(self.raw_dir, base + ".md")
        counter = 2
        while os.path.exists(path):
            path = os.path.join(self.raw_dir, "%s-%d.md" % (base, counter))
            counter += 1

        header = "\n".join([
            "---",
            "title: %s" % title,
            "source: %s" % source,
            "ingested: %s" % datetime.now(timezone.utc).isoformat(),
            "---",
            "",
        ])
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(header + content.strip() + "\n")
        return os.path.relpath(path, self.root)

    # --- wiki writes ------------------------------------------------------

    def write_page(self, page: WikiPage, subdir: str = "") -> str:
        if page.page_type not in VALID_TYPES:
            raise ValueError("unknown page type %r" % page.page_type)
        if page.evidence not in VALID_EVIDENCE:
            raise ValueError("unknown evidence label %r" % page.evidence)
        if page.evidence == "sourced" and not page.sources:
            raise ValueError("a sourced page must cite at least one raw/ file")

        self.ensure()
        directory = os.path.join(self.wiki_dir, subdir) if subdir else self.wiki_dir
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, slugify(page.slug) + ".md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(page.render())
        return os.path.relpath(path, self.root)

    def log_event(self, text: str) -> None:
        """Append a line to wiki/log.md: the timeline of what the desk learned."""
        self.ensure()
        path = os.path.join(self.wiki_dir, "log.md")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        exists = os.path.exists(path)
        with open(path, "a", encoding="utf-8") as handle:
            if not exists:
                handle.write("# Log\n\nTimeline of what the desk learned, newest last.\n\n")
            handle.write("- **%s** %s\n" % (stamp, text.strip()))

    def pages(self) -> List[str]:
        found: List[str] = []
        for base, _dirs, files in os.walk(self.wiki_dir):
            for name in files:
                if name.endswith(".md"):
                    found.append(os.path.join(base, name))
        return sorted(found)

    def sources(self) -> List[str]:
        if not os.path.isdir(self.raw_dir):
            return []
        return sorted(
            os.path.join(self.raw_dir, f)
            for f in os.listdir(self.raw_dir)
            if f.endswith(".md")
        )

    # --- lint -------------------------------------------------------------

    def lint(self, stale_days: int = 30) -> List[LintFinding]:
        """Health check. Unsourced claims and dead links are the real targets.

        A wiki that is never linted drifts into confident fiction, which on a
        trading desk is indistinguishable from lying to yourself.
        """
        findings: List[LintFinding] = []
        page_paths = self.pages()
        known_sources = {os.path.relpath(p, self.root) for p in self.sources()}
        slugs = {os.path.splitext(os.path.basename(p))[0] for p in page_paths}
        linked: set = set()

        for path in page_paths:
            rel = os.path.relpath(path, self.root)
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()

            meta = parse_frontmatter(text)
            if not meta:
                if os.path.basename(path) not in ("index.md", "log.md", "README.md"):
                    findings.append(LintFinding("error", rel, "missing frontmatter"))
                continue

            page_type = meta.get("type", "")
            if page_type not in VALID_TYPES:
                findings.append(LintFinding("error", rel, "invalid type %r" % page_type))

            evidence = meta.get("evidence", "")
            cited = _parse_list(meta.get("sources", ""))
            if evidence == "sourced" and not cited:
                findings.append(
                    LintFinding("error", rel, "labelled sourced but cites nothing")
                )
            for citation in cited:
                if citation and citation not in known_sources:
                    findings.append(
                        LintFinding("error", rel, "cites a missing source: %s" % citation)
                    )

            updated = meta.get("updated", "")
            age = _age_days(updated)
            if age is not None and age > stale_days:
                findings.append(
                    LintFinding("warn", rel, "not updated in %d days" % age)
                )

            for target in re.findall(r"\[\[([^\]]+)\]\]", text):
                linked.add(slugify(target.split("|")[0]))

        for slug in sorted(linked - slugs):
            findings.append(
                LintFinding("warn", "wiki/index.md", "link to a page that does not exist: %s" % slug)
            )

        orphans = slugs - linked - {"index", "log", "readme"}
        for slug in sorted(orphans):
            findings.append(LintFinding("warn", "wiki/%s.md" % slug, "orphan page: nothing links here"))

        return findings

    # --- index ------------------------------------------------------------

    def rebuild_index(self) -> str:
        """Regenerate wiki/index.md as the catalog of every page."""
        self.ensure()
        grouped: Dict[str, List[str]] = {}
        for path in self.pages():
            name = os.path.basename(path)
            if name in ("index.md", "log.md"):
                continue
            with open(path, "r", encoding="utf-8") as handle:
                meta = parse_frontmatter(handle.read())
            page_type = meta.get("type", "unfiled")
            rel = os.path.relpath(path, self.wiki_dir)
            subject = meta.get("subject", os.path.splitext(name)[0])
            evidence = meta.get("evidence", "?")
            grouped.setdefault(page_type, []).append(
                "- [%s](%s) — %s" % (subject, rel, evidence)
            )

        lines = [
            "# Index",
            "",
            "Catalog of every wiki page. Regenerated by `Brain.rebuild_index()`.",
            "",
            "Numbers live in `ledger/`, which is append-only. Pages here interpret",
            "the ledger; they never restate it as fact.",
            "",
        ]
        if not grouped:
            lines.append("_No pages yet. Ingest a source into `raw/` to start._")
        for page_type in sorted(grouped):
            lines.append("## %s" % page_type)
            lines.append("")
            lines.extend(sorted(grouped[page_type]))
            lines.append("")

        path = os.path.join(self.wiki_dir, "index.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines).rstrip() + "\n")
        return os.path.relpath(path, self.root)


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "untitled"


def parse_frontmatter(text: str) -> Dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    meta: Dict[str, str] = {}
    for line in text[3:end].strip().splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta


def _parse_list(value: str) -> List[str]:
    value = value.strip().strip("[]")
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _age_days(updated: str) -> Optional[int]:
    try:
        when = datetime.strptime(updated, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc) - when).days


__all__ = ["Brain", "WikiPage", "LintFinding", "slugify", "parse_frontmatter"]
