# raw/ — immutable sources

Drop sources here. Articles, filings, exchange exports, research notes, pasted
threads, your own trading observations.

Rules:

1. **Never edited after landing.** A source is evidence. If it was wrong, the
   correction is a new file, not a rewrite.
2. **Filename is `YYYY-MM-DD--slug.md`.**
3. **Every file starts with frontmatter naming where it came from:**

```
---
title: FOMC September minutes
source: https://www.federalreserve.gov/...
ingested: 2026-09-09T18:00:00+00:00
---
```

`Brain.ingest()` handles the naming and the header for you.

Anything in `wiki/` that claims to be `sourced` must cite a file in here. That
citation is checked by `Brain.lint()`, so an uncited claim gets demoted to
`assumption` rather than quietly passing as fact.
