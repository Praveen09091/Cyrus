# Log

Timeline of what the desk learned, newest last. Cyrus writes here; you read it.

- **2026-09-09** Desk scaffolded. Ten seats defined, message contract fixed, risk kernel made deterministic. Mode is paper and there is no live venue wired.
- **2026-09-09** Mean reversion gained a `min_edge_atr` filter. A z-score is scale-free, so a dead-quiet series was throwing large readings on pure noise and raising proposals worth less than the spread. The filter requires the trip back to the mean to beat one bar of normal movement.
- **2026-09-09** Synthetic `^VIX` given its own profile. Without it the generic base of 100 read as a permanent volatility crisis and pinned Atlas to a stress regime on every offline run.
