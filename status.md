# Build status

**Updated:** 2026-08-15 · **Plan:** [PLAN.md](./PLAN.md) · **Brief:** [objectives.md](./objectives.md)

Phase P0 (setup) and P1 (the warehouse world) are done. Nothing reads labels or makes
decisions yet — that is P2 and P3, next.

```
P0 setup        [####################] done
P1 world        [####################] done
P2 evidence     [                    ] next
P3 belief       [                    ]
P4 decisions    [                    ]
P5 ledger       [                    ]
P6 simulation   [                    ]
P7 demo screen  [                    ]
P8 video        [                    ]
```

**Current state:** 29 tests pass, ruff clean, mypy strict clean.

---

## Done

### P0 — Project setup

- Installed `uv` (0.12.5) and Python 3.12.14. The machine only had Python 3.9, which is
  too old for the syntax used here.
- `pyproject.toml` with the dependency groups from the plan: core (pydantic, numpy,
  pandas, scipy, pillow, pyyaml, rich), plus optional `llm`, `demo` and `dev` groups.
- Directory layout matches the plan. Package folders: `world`, `services`, `agent`,
  `ledger`, `downstream`, `harness`, `common`.
- Linting and type checking wired up: ruff (line length 100) and mypy in strict mode.

**Run it:**

```bash
cd /Users/rmody/LEC_AI
.venv/bin/python -m pytest      # tests
.venv/bin/ruff check .          # lint
.venv/bin/mypy                  # types
```

### P1 — The warehouse world

**`common/coding.py`** — batch code format and check digits.

A batch has a short id (`B-2288`). The carton prints the full code `B-2288-0`, where the
last digit is a check digit over the four body digits. This lets a reader tell "I misread
a character" apart from "this label genuinely says something else".

**Finding that changed the plan:** changing any single body digit *always* breaks the
check digit. I verified this exhaustively across all positions and digits. This is what
check digits are for, but it made the original test case S6 impossible, because S6 assumed
one character could be misread while the check digit stayed valid for two different
batches. S6 has been redesigned — see below. PLAN.md has been corrected.

**`world/types.py`** — the ground truth entities: Sku, Batch, Bin, Shipment, Customer,
ReturnEvent, World. All Pydantic models.

`ReturnEvent` holds both `true_batch_id` (the real answer) and `printed_code` (what is
physically on the carton). These differ in the hero case, which is the whole point.

**`world/generators.py`** — one fixed, hand-checkable warehouse state. Deterministic, no
random seed, so the worked example in PLAN.md section 6.5 stays valid.

- 5 batches of one product, 7 bins (including a holding bin and a quarantine bin)
- 4 customers, one of which repacks goods (that is the one whose labels lie)
- 8 shipments
- 6 return events, one per test case

**`world/labels.py`** — draws carton labels as real PNG images, then damages them.

The damage is genuine image damage — glare, blur, smear, torn paper, sensor noise — not a
flag on a data structure. Whatever reads these has to cope with actual missing characters.
Five damage profiles, each targeting a specific part of the label.

### Test cases built

| Case | Return | True batch | Label shows | Label damage |
|---|---|---|---|---|
| Case | Return | Customer | True batch | Label shows | Label damage |
|---|---|---|---|---|---|
| S1 clean | RET-S1 | CUST-455 | B-2293 | `B-2293-2`, correct | none |
| S2 unreadable label | RET-S2 | CUST-455 | B-2293 | `B-2293-2`, correct | water damage, code destroyed |
| S3 nothing to go on | RET-S3 | CUST-337 | B-2296 | `B-2296-9`, correct | glare over code *and* date |
| **S4 hero** | RET-S4 | CUST-118 | **B-2288** | **`B-2291-4`, wrong batch** | **none — crisp and valid** |
| S5 both sources bad | RET-S5 | CUST-118 | B-2290 | `B-2290-5`, correct | glare on the last digits |
| S6 near-miss twins | RET-S6 | CUST-204 | B-2291 | `B-2291-4`, correct | torn away: reads `B-229` only |

The rendered labels are in `artifacts/labels/`. I checked each one by eye:

- **S4** renders crisp and completely convincing: `B-2291-4`, best before 15 MAR 2027,
  consignee CUST-118. Every visible signal says trust it. It is wrong.
- **S2 and S3** are genuinely unreadable in the code region — confirmed by measuring the
  fraction of dark pixels there, not by assumption.
- **S6** reads `B-229` with the last digit and check digit torn away, so it fits both
  B-2290 and B-2291. Both were shipped to that customer.

Two damage profiles needed strengthening after looking at them: the first attempt at
"water damage" and "heavy glare" still left the code readable. S3 also needed the glare
extended over the best-before date, because the date alone identified the batch and the
case is supposed to leave the agent with nothing.

### The torn label (S6), rebuilt

The first version just painted a pale patch over the last digits. It read as an erasure,
not a tear. Rewritten so it now:

- rips a piece out of the label from the right-hand edge inwards, with a ragged boundary
  made from two wavelengths of wobble plus per-point jitter — top, bottom and left edges
  are all irregular
- exposes kraft carton underneath, with mottled colour and a few faint creases, stencilled
  through a mask so the texture cannot leak outside the torn area
- shows pale paper fibres along the break, with individual fibres straddling the edge, and
  a shadow on the carton from the label's own thickness

The profile was renamed from `torn_corner` to `torn_piece`, because it takes a piece out
of the side rather than a corner.

**Two problems this uncovered.**

1. The tear has to destroy the best-before date as well as the code. B-2290 expires
   November 2026 and B-2291 expires March 2027, so a legible date would identify the batch
   outright and the case would not be ambiguous at all. The rip now covers both.
2. `B-229` matches **four** batch codes (B-2290, B-2291, B-2293, B-2296), and CUST-204 had
   been sent three of them — so S6 had three candidates, making it a repeat of S3. Added a
   fourth customer, CUST-455, and moved the clean cases S1 and S2 onto it. CUST-204 now
   receives only B-2290 and B-2291, so S6 has exactly two candidates as intended.

Four tests now hold the tear to its job: the start of the code survives, the tail is gone,
the date is gone, carton is genuinely exposed and does not bleed outside the rip, and the
rip boundary wanders rather than running straight.

One measurement subtlety: the first version of these tests counted any dark pixel as ink,
which counted the exposed carton as legible text and left almost no margin. The tests now
require ink to be dark *and* neutral in colour, since kraft is warm brown. Margin went from
0.0049 against a 0.005 limit to a clean 0.0.

### Tests written (29, all passing)

- `test_coding.py` — check digit round-trips, known values, and the exhaustive proof that
  a single digit change always breaks the check digit.
- `test_world.py` — nothing ships before quality release; every return is genuinely
  partial; every true batch really was sent to that customer; the hero case is actually
  misleading (valid label, wrong batch, overstates shelf life by exactly 166 days, and the
  labelled batch was released *after* the shipment left).
- `test_labels.py` — measures ink in the code region to confirm damage profiles do what
  they claim; confirms rendering is byte-for-byte repeatable.
- `test_isolation.py` — `agent/` must never import `world/`. Without this the agent could
  read the answers directly and every harm number would be worthless. The test also checks
  itself, so it cannot pass by parsing nothing.

---

## Next steps

### P2 — Evidence sources (next)

- [ ] Write the SQLite schema and load the world into it
- [ ] `services/wms_client.py` — query shipments by customer and product
- [ ] Fault injection for the warehouse system: stale replica, duplicate rows, timeout,
      out-of-order timestamps
- [ ] `services/label_reader.py` — read a damaged label image and return a structured
      result: characters read, per-character confidence, check digit pass/fail, quality
      warnings
- [ ] Decide how the label reader runs offline. Plan is Claude vision with recorded
      responses committed to `tests/cassettes/`, so the demo and the tests need no network
      and no API key
- [ ] `services/batch_registry.py` and `services/shipment_ledger.py` — the paid lookups,
      each with a price and a small failure rate
- [ ] `config/scenarios/*.yaml` — one file per case, naming the return, the label damage,
      and which faults are switched on
- [ ] Tests: each source fails on its own, with a named symptom

### P3 — Working out probabilities

- [ ] `agent/evidence.py` — what the agent is allowed to see
- [ ] `agent/confusion.py` — which characters look alike (`1/l/I`, `0/O/D`, `5/S`, `8/B`)
- [ ] `agent/reliability.py` — failure rates per source per symptom, stored as counts
- [ ] `agent/hypotheses.py` — build the candidate list, including the "none of the above"
      catch-all
- [ ] `agent/belief.py` — the Bayes update, with source failure as part of the maths
- [ ] Test: reproduce PLAN.md section 6.5 exactly — 43.5% after the label, 63.1% after the
      registry, 86.7% after the note

### P4 — Making decisions

- [ ] `config/harm.yaml` — the cost table
- [ ] `agent/harm.py`, `agent/voi.py` (is a lookup worth buying), `agent/policy.py`
- [ ] `agent/loop.py` — the two decisions, with spend and time limits
- [ ] `agent/trace.py` — write the decision log
- [ ] Test: no default branch. Editing the cost file must flip the chosen action on
      identical evidence

### P5 — Stock ledger

- [ ] Append-only movements, reversible, each pointing back to the decision that caused it
- [ ] Drift measurement against ground truth
- [ ] Property tests: units in equals units out, no negative stock

### P6 — Proving the harm (do not skip)

- [ ] Demand generator, first-expired-first-out picker, reordering, forecasting
- [ ] 180-day run, all policies, 200 seeds, confidence intervals
- [ ] `test_harm_is_real.py`
- [ ] **Check the 180-day horizon is long enough.** If misfiled stock never gets picked
      inside the window, the headline harm is zero and the whole proof collapses. This is
      the biggest open risk in the plan

### P7 — Demo screen

- [ ] Streamlit page: carton image, probability bars, cost table, harm charts
- [ ] Must run offline in under 45 seconds

### P8 — Video

- [ ] Follow the shot list in PLAN.md section 13

---

## Open questions

1. **Is 180 days long enough for the simulation?** Unresolved, and it is the main risk.
   Check in P6 before tuning anything else.
2. **Should the reliability model start from a synthetic history or from nothing?** Leaning
   towards a committed synthetic history of past returns, so the numbers are inspectable.
3. **Add a second label reader?** Running a traditional text-recognition tool alongside the
   vision model would give two readers that fail in different ways. Only if P2 finishes
   early.
4. **Should a return be allowed to split across two batches?** Realistic, but it enlarges
   the problem. Deferred until P6 is passing.

---

## Notes for later

- The `common/` package holds things both the world and the services need (currently just
  the check digit logic). The agent may import `common`, but never `world`.
- `artifacts/labels/` is regenerated by `world.labels.render_all`. The images are cheap to
  rebuild, so they do not need committing unless the video needs a frozen copy.
