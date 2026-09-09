# ledger/ — append-only trade record

Written by the kernel. Read by every seat. **Edited by nobody.**

- `journal-YYYY-MM.jsonl` — proposals, verdicts, orders, fills, decisions,
  alerts, and closed trades, one JSON object per line.
- `bus/bus-YYYY-MM-DD.jsonl` — every envelope that crossed the bus, journaled
  before subscribers run, so a crash mid-cycle still leaves a readable trail.

Why this is separate from `wiki/`: the wiki is rewritable by a language model.
A model that can revise its own trade record cannot be trusted to report on its
own performance. So the numbers live here, and the wiki is only ever allowed to
interpret them.

Rejections are recorded alongside fills. They are the more valuable half: the
record of what the desk refused, and why, is what turns a loss into a rule.
