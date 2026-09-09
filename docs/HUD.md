# HUD specification: hierarchy, motion, and signal vocabulary

The interface has one job: make Cyrus's reasoning inspectable, and make a veto
impossible to miss. It is a window into the desk, not a trading terminal. There
is no buy button, because a human clicking buy would bypass the risk kernel.

This document is the contract the frontend implements. The backend already
emits everything listed here on the bus.

## 1. Visual hierarchy

Three rings, matching the authority model.

```
                    ┌─────────────────┐
                    │   YOU (board)   │   approvals, mode changes, kill switch
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │      CYRUS      │   the only voice. centre of the HUD.
                    └────────┬────────┘
        ┌────────────────────┼────────────────────┐
        │                    │                    │
   INTELLIGENCE          ANALYSIS            RISK + EXECUTION
   atlas scout athena    chartist oracle     sentinel pilot
        │                    │                    │
        └────────────────────┴────────────────────┘
                             │
                    ┌────────▼────────┐
                    │ LEDGER · TREASURY│  the record, always visible
                    └─────────────────┘
```

Layout rules:

- **Cyrus sits at the centre.** Every other seat is a satellite. Nothing else
  gets the centre, ever, because nothing else speaks to the user.
- **Sentinel is visually separate from the proposing seats.** It reports to the
  human, not to Cyrus, and the layout must show that. Give it its own gutter or
  its own colour channel.
- **Pilot sits behind Sentinel, never beside it.** The order of the two on
  screen encodes that execution comes after authorisation.
- **The ledger is persistent chrome**, not a tab. Equity, drawdown against the
  flatten limit, open positions, and burn coverage are always on screen.

## 2. Seat states

Every seat is in exactly one state. The bus message that causes each transition
is named, so the frontend never has to infer.

| State | Meaning | Triggered by |
| --- | --- | --- |
| `idle` | No work this cycle | end of cycle |
| `scanning` | Pulling data | `ScanRequest` |
| `thinking` | Producing a finding | between request and emit |
| `speaking` | Published a finding | `Observation`, `Proposal` |
| `approving` | Voted yes | `Vote(approve)` |
| `dissenting` | Voted no | `Vote(reject)` |
| `abstaining` | No basis to judge | `Vote(abstain)` |
| `vetoing` | Rejected an order | `Verdict(rejected)` |
| `authorising` | Stamped an order | `Verdict(approved)` |
| `executing` | Order in flight | `OrderIntent` |
| `halted` | Kill switch tripped | `Alert(halt / flatten)` |

`abstaining` must be visually distinct from `approving`. An abstention is a
seat saying "I have no data", and it does not count toward quorum. Collapsing
it into a neutral grey that reads like assent would misrepresent the decision.

## 3. Motion

Motion carries meaning here. Every animation maps to a state change on the bus,
and nothing moves decoratively.

### Tokens

| Token | Duration | Easing | Used for |
| --- | --- | --- | --- |
| `pulse-think` | 1400ms loop | `ease-in-out` | a seat working |
| `emit-flare` | 220ms | `ease-out` | a finding published |
| `vote-settle` | 180ms | `ease-out` | a vote landing |
| `veto-slam` | 120ms | `ease-in` | Sentinel rejecting |
| `auth-stamp` | 300ms | `cubic-bezier(.2,.8,.2,1)` | authorisation issued |
| `order-travel` | 500ms | `linear` | intent moving to the venue |
| `flatten-sweep` | 700ms | `ease-in-out` | flatten switch firing |
| `equity-tick` | 400ms | `ease-out` | equity number changing |

### Rules

1. **Veto is the fastest animation on screen.** `veto-slam` at 120ms with no
   ease-out. A rejection should feel like a door, not a fade. It is the most
   important event the desk produces and it must never look like a soft
   suggestion.
2. **Authorisation is slower than rejection.** `auth-stamp` at 300ms with a
   settle. Committing capital should feel deliberate.
3. **`flatten-sweep` interrupts everything.** When the drawdown switch fires,
   cancel every in-flight animation, dim all proposing seats, and run one sweep
   across the whole board. The desk is not thinking any more; it is leaving.
4. **Data flows along the edges.** A finding travels from the seat that produced
   it to Cyrus. An order travels from Cyrus through Sentinel to Pilot and only
   then off-board. The path is the audit trail, drawn.
5. **No idle animation.** An idle desk is still. Ambient motion trains the eye
   to ignore movement, which is exactly the wrong reflex when a veto lands.
6. **Respect `prefers-reduced-motion`.** Replace every animation with an
   instant state change. No information may live only in motion.

## 4. The decision trace

The primary view is not a chart. It is one decision, expanded.

```
SPY  long  15m                              cycle cyc_94ba5ceebb
─────────────────────────────────────────────────────────────────
thesis        2.1 sd below the 20-period mean; reversion to 498.30
bear case     the stretch is the first leg of a trend
invalidation  close below 495.10  (2.0 ATR)

atlas      approve   regime risk_on does not contradict mean reversion
scout      approve   liquidity clears at $2.1B average dollar volume
athena     approve   calendar clear for 48h
chartist   approve   stop sits 2.0 ATR from entry
oracle     abstain   no realised record for mean_reversion_z
─────────────────────────────────────────────────────────────────
SENTINEL   REJECTED  no_quorum:4_of_3 ... size 0
DECISION   stood down. cash is a position.
```

Every field above comes from a real message: `Proposal.thesis`,
`Proposal.bear_case`, `Proposal.invalidation`, `Vote.reason`,
`Verdict.reasons`, `Decision.rationale`.

The bear case is rendered at the same weight as the thesis. Never smaller,
never collapsed by default. A one-sided idea is advertising.

## 5. Indicator and signal display

| Element | Shows | Never shows |
| --- | --- | --- |
| Price panel | Bars, entry, stop, target, invalidation level | a projection line |
| Indicator strip | The indicators the book's style actually uses | every indicator available |
| Regime badge | Atlas's regime plus the inputs behind it | a sentiment score with no source |
| Spectral panel | Dominant period, out-of-sample R², wrap-cliff artifact share | an in-sample fit on its own |
| Feed label | `REALTIME` or `DELAYED`, per instrument | nothing |

Two hard rules:

- **The feed label is always visible.** Free data is delayed. A HUD that hides
  that invites the user to react to a stale price.
- **The spectral panel leads with the out-of-sample number.** The in-sample fit
  is displayed smaller, next to the artifact share. A beautiful in-sample
  reconstruction is a warning, and the layout must say so.

## 6. Colour

| Meaning | Role |
| --- | --- |
| Approve / healthy | one accent, used sparingly |
| Dissent / veto | the loudest colour on the board |
| Abstain / no data | distinct from both, clearly "unknown" |
| Halted / flattened | takes over the frame, not a badge |
| Paper mode | a permanent marker while `mode: paper` |

The paper-mode marker stays on screen the entire time the desk is on paper.
The moment a user forgets they are looking at simulated fills is the moment the
numbers start lying to them.

## 7. What the HUD must never do

- No buy, sell, or size control. Orders come from the kernel or not at all.
- No way to widen a limit from the UI. Limits change in `config/desk.yaml`,
  under review, in git.
- No hiding a rejection. Rejections are the product.
- No number without provenance. Every figure traces to the ledger or a feed.
