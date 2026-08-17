# Reading the screen

**Repo:** [README.md](../README.md) · **Progress:** [status.md](../status.md)
· **Video script:** [SCRIPT.md](./SCRIPT.md)

```bash
uv sync --extra demo
uv run streamlit run demo/app.py
```

No browser? `uv run python -m demo.run S4` prints the same walkthrough in a terminal, with no
extra dependencies.

It opens on **S4**, the case the whole project is about. Everything runs offline: no network,
no model call, no API key. Building all twelve cases takes well under a second.

---

## 1. What you are looking at

Four panes. Read them clockwise from the top left.

```
┌──────────────────────────┬──────────────────────────┐
│  THE CARTON              │  WHAT IT MIGHT BE        │
│  photo, code read,       │  probability per          │
│  confidence, check digit │  candidate, after each    │
│  damage symptoms         │  piece of evidence        │
├──────────────────────────┼──────────────────────────┤
│  DECISION 1 & 2          │  WHAT THIS COSTS LATER   │
│  every action, priced,   │  filed vs truth, drift    │
│  the winner, the margin  │  ── then, separately, ──  │
│                          │  how the policy does      │
│                          │  overall (not this case)  │
└──────────────────────────┴──────────────────────────┘
```

### Top left — the carton

The photograph is a real PNG that was drawn and then damaged; the reading below it came from a
vision model looking at that image, recorded once and replayed.

- **Code read** — what was legible. May be a fragment (`B-22…`) or nothing at all.
- **Check digit** — `valid`, `FAILED`, or `not checked`. A failure means the characters do not
  add up, so *something* was misread. Validity does **not** mean the label is telling the truth
  about what is in the box.
- **Warning signs** — glare, blur, a torn edge, ink bleed. These pick which reliability bucket
  applies.
- **The blue box** appears when the label is crisp, complete and passes its check digit. That
  is the trap: everything about it says trust it.

### Top right — what it might be

One bar per candidate, plus **`other`** — the catch-all, meaning "it might be a batch nobody
listed". Below, a row per piece of evidence showing how the probabilities moved as each arrived.

Watch the **`other`** column. If it stays high, the agent is telling you the answer may not be
on the list at all. If it collapses to near zero, the agent is claiming the answer is among the
names shown — and if it is wrong about that, everything downstream is wrong with it.

**A flat chart is not a malfunction.** The amber "no candidate is clearly ahead" note means the
evidence genuinely does not separate the options. That is the honest state, and it is what
makes paying for a lookup or calling a person worth doing.

### Stepping through the evidence

Tick **"step through the evidence"** in the sidebar and a slider appears. It replays the case
one source at a time: the belief after each piece, what that piece said about each candidate,
and — the useful part — **every action priced with only the evidence up to that point.**

A final screenshot cannot show a contest. It shows one winner. This shows the contest.

On the hero case the most likely answer **changes three times**:

| Step | After | Leader | What just happened |
|---|---|---|---|
| 0 | nothing | `B-2291` 51% | The prior. There is simply more of this batch in the building |
| 1 | shipment records | `B-2288` 52% | The records overturn it |
| 2 | the carton label | `B-2291` 61% | **The trap.** A clean, valid label drags it back to the wrong answer |
| 3 | the note | `B-2291` 61% | Nothing here; no lot code in this one |
| 4 | constraints *(paid)* | `B-2288` 58% | The registry: `B-2291` cleared quality control after the shipment left |
| 5 | dispatch *(paid)* | `B-2288` 59% | Door scans agree |
| 6 | print date *(paid)* | `B-2288` 84% | Settled — and the best action changes from escalate to commit |

Two banners fire as you drag:

- **Amber — "the most likely answer just changed"**, at steps 1, 2 and 4. Nothing was
  reconsidered or retried; one new piece of evidence moved it.
- **Green — "the best action just changed"**, at step 6. Escalate becomes commit. That is
  "genuinely competing strategies, decided at runtime" as a thing you can watch rather than a
  claim in a README.

Steps needing a paid lookup are marked. Knowing is not free.

**One honest caveat, stated on the pane itself:** "if it had to place the stock now" is not
quite the question the agent asks at its first decision, which *also* weighs whether to buy
more evidence. It is priced with the same code, but it is a slightly different question and the
screen says so rather than blurring the two.

### Bottom left — the cost table

**This is the pane that carries the argument.** Every action the agent could take, what each
would cost in expected pounds, the fee it would pay, and a tick against the one it chose.

- The chosen row is always the cheapest. There is no `else` branch anywhere; escalating to a
  person is an option in this table like any other, and wins when it is genuinely cheapest.
- **"Ahead by £X"** is the margin over the next option that would lead somewhere *different*.
  Two ways of buying lookups that both end at the same next step are not a real alternative, so
  they do not count.
- **The amber "close call"** means the margin is under 5%. At that distance the answer rests on
  a cost figure somebody chose rather than on the evidence, and the screen says so instead of
  presenting a coin flip as a clear call. Six of the twelve recorded cases are close calls;
  all six are the same choice — escalate, or pay 30p for a lookup.
- **Expected cost** is the harm risked plus any fee; **fee** is only what the action costs to
  take, and only a lookup has one. Section 4 sets the two out in full.
- **The sensitivity table** below says how far one cost figure would have to move to change the
  answer. If it is empty, no single figure can be moved to a sensible value that would flip it.

Both decisions appear. The second uses the probabilities the first produced, and after a
lookup they are usually very different.

### Bottom right — what it costs later

The immediate consequence: what the stock was filed as, what it really was, and the drift that
leaves in the record. Green means no drift at all. Red means the recorded expiry is wrong, and
by how many days.

Below a divider, in its own section, the committed simulation: six policies over 600 runs of
540 days each. **That block is not about the case on screen** — it is a whole-operation average
and is identical whichever case you are looking at. It is separated and labelled for exactly
that reason; sitting next to a single return it would read as a claim about that return.

Expand **"stock movements this decision caused"** to see the two ledger rows: the receipt, and
the placement, each naming the decision that caused it. That link is what makes any wrong
number traceable and reversible.

---

## 2. The two kinds of case

The sidebar switches between them, and the difference is the point.

### Recorded — twelve, written by hand

| Case | What it is |
|---|---|
| **S4 — the obvious answer is wrong** | The hero case. Crisp label, valid check digit, and the stock is a different batch |
| S1 clean | Everything agrees. The easy case, for contrast |
| S2 unreadable label | Water damage; the code cannot be read |
| S3 nothing to go on | Heavy glare and unhelpful records |
| S5 both sources degraded | Neither the label nor the records can be trusted |
| S6 near-miss twins | A torn corner; the surviving fragment fits two batches |
| S7 the answer is only in the note | Cross-docked stock; the batch appears only in prose |
| S8 corrupted, not unreadable | Ink bleed turned one digit into a lookalike |
| X label reader down | The reader itself fails |
| X warehouse timeout | No records and no batch list at all |
| X contradictory rows | The records disagree with themselves |
| X paid lookup down | The external source is unavailable |

These have real photographs and real recorded model readings, so they are the only cases that
show **perception** working.

They also have a weakness worth stating out loud: **the agent was built while looking at them.**
They can show that it handles what it was shown. They cannot show that it handles anything else.

### Generated — made up a moment ago

Pick "Generated now" and a seed. A fresh warehouse, fresh batches with a spread of expiry dates,
fresh shipments, a random fault on each source, and an answer known only to the scoring code.
The warehouse, the SQLite database, the shipment records, the batch registry and the paid
lookups are all real and read through the same service classes with the same fault switches.

**There is no photograph** for these, and the screen says so. The label reading is constructed
and put through the same validation code rather than being read off an image — rendering and
reading thousands of images is not practical. So generated cases show the *reasoning* under
uncertainty; the recorded twelve show the perception.

The **"world matches the reliability model"** tick is worth understanding:

- **Ticked** — faults happen at the rates the agent believes they do. A failure here is a
  reasoning failure, because its beliefs match the world it is in.
- **Unticked** — every fault is equally likely, which is not what it believes. Failures then
  mean the beliefs are wrong, not the reasoning. The agent does visibly worse, and that gap is
  the measured cost of the reliability numbers being hand-set.

### Seeds worth trying

| Seed | What it shows |
|---|---|
| **418** | **The agent failing, honestly.** A reused label and a stale replica, so nothing anywhere names the true batch. It commits confidently and is wrong by 253 days |
| 52 | A reused label with the warehouse system down. No batch list, so it holds the stock rather than guessing |
| 24 | 441 units and the label is **destroyed**. It escalates: past about a hundred units a person costs less than the risk left after a lookup |
| 7 | 1,192 units, clean evidence, and it commits — correctly. Large does not mean timid |
| 99, 1001 | Ordinary cases, for contrast |

Same seed, same case, every time — so a demo can be rehearsed.

---

## 3. What good and bad look like

**Signs the agent is behaving:**

- The chosen row in the cost table is the cheapest one. Always.
- `other` stays meaningfully above zero when the evidence is thin.
- When it cannot tell, it holds or escalates rather than guessing.
- A hold is dated at the earliest expiry any batch of that product has — never later than the
  stock could really last.
- It never pays for a lookup and then escalates anyway. That would be money for nothing.

**Signs something is wrong, if you ever see them:**

- A probability of exactly 0 or exactly 1.
- `other` collapsing to near zero while the evidence is obviously poor.
- A large return filed under a batch with no lookup bought.
- A recorded expiry *later* than the truth. This is the failure the whole project exists to
  avoid, and it should never appear on a held or escalated case.

---

## 4. Reference — every field on the screen

### Expected cost vs fee (bottom left)

Two different things, and the distinction is the whole design.

- **Expected cost £** — everything the action is expected to cost, in pounds: the harm it
  risks, weighted by how likely each candidate is, *plus* any fee. This is the number the
  agent compares, and the cheapest one always wins.
- **Fee £** — what the action costs to *take*, before any harm. Only a lookup has one:
  **£0.30** for the batch registry, **£0.40** for the shipment ledger, £0.70 for both.
  Committing, holding and escalating are all £0.00 to take — their cost is entirely the harm
  they risk.

So a row reading `gather batch_registry · expected £28.11 · fee £0.30` means: pay 30p now, and
expect to be £28.11 worse off in total once the consequences of whatever you do next are
priced in. Escalating at £29.50 with a £0.00 fee costs nothing to do and £29.50 in expected
consequences — mostly the analyst's time and the share of returns a person still gets wrong.

The **exposure** behind each row is in the trace (`artifacts/traces/*.json`): how many units of
each kind of harm the action is exposed to, before those units are priced.

### The evidence rows (top right)

One row per update, in the order they were applied. Not every case has all of them.

| Row | What it is | Needs a lookup? |
|---|---|---|
| `records` | Shipment records from the warehouse system — what we think we sent this customer | no |
| `label` | The carton label: the characters read, the check digit, and the chance the label is genuine but on the wrong box | no |
| `note` | A **lot code written out in prose** in the condition note. The one thing here no database query can do | no |
| `constraints` | Physical impossibilities — a batch that cleared quality control *after* the shipment left cannot have been in it; a batch never allocated to this customer | yes (registry) |
| `dispatch` | What the paid lookups say actually left the door | yes |
| `note dates` | A print date stamped on the goods, matched against when each batch was made | yes (registry) |

The first three are free and always applied. The last three only appear once the agent has
decided a lookup is worth buying — which is why a case that escalates immediately shows only
three rows.

### Reader confidence (top left)

**It is the vision model's own self-report**: a 0–1 number it returns alongside the characters,
saying how sure it is of what it reported. Our code does not compute it.

That matters, and it is worth saying plainly: a self-reported confidence is not a calibrated
probability, and nothing here treats it as one. It is used as a **symptom**, not as a
probability. Below 0.70 the reading is tagged `low_confidence`, which puts it in a different
reliability bucket, and it is the *bucket* — not the number — that carries the failure rates
the agent reasons with.

The buckets, most specific first: `no_code_found`, `check_digit_failed`, `incomplete_code`,
`low_confidence`, `clean_valid_repacker`, `clean_valid_standard`. The last two are the
interesting pair: an identical, perfect reading is treated differently depending on whether the
customer is known to repack goods, because that is what makes a genuine label end up on the
wrong box.

### "How far a cost would have to move to change this"

The parameters are the entries in the cost table (see below). For each one:

- **now_gbp** — what that figure is set to today.
- **flips_at_gbp** — the value at which the chosen action stops being the cheapest.
- **slack_x** — how many times the figure would have to change to get there.

A row reading `expired_unit · now 48.00 · flips at 39.35 · slack 1.2x` means: shipping an
expired unit is priced at £48, and if it were really below about £39 the agent would have
chosen differently. A slack of 1.2× is thin; a slack of 10× means the decision does not rest on
that number at all.

An **empty table is a good sign** — it means no single cost figure can be moved to any sensible
value that would change the answer.

### The policies in "how the policy does overall"

These are what the agent is measured against. Only the first four are things anyone could
actually run.

| Policy | What it does |
|---|---|
| **agent** | RECONCILE: probabilities, then the cheapest action |
| **trust the label** | Read the box, believe the box. What most warehouses do, and right most of the time — which is exactly why its failures go unnoticed |
| **trust the records** | Believe the warehouse system: file it as whatever we shipped most of |
| **always escalate** | Send every return to a person. Not safe: a 1% human error rate applied to *every* return ships more expired stock than the agent does |
| **always segregate** | Hold everything back under the earliest expiry any batch could have. Ships almost no expired stock and costs £59,000 more per run, because held stock is never on a pick face when it is needed |
| **oracle** | **Not a policy anyone can run — it knows the answer.** It is the floor. Everything else is measured against it rather than against zero — see below for why it is not zero itself |

### Why the oracle costs anything at all

It knows the answer, so it ships **zero** expired units, escalates nothing, buys no lookups and
strands no stock. Its entire cost — about £137,000 a run — is stock written off when its
recorded best-before passes on the shelf.

That write-off has nothing to do with identifying returns. It comes from the **replenishment
rule**: the simulation reorders up to a fixed level with no forecasting, so long-dated stock
sits behind shorter-dated deliveries in the first-expired-first-out queue and some of it ages
out before it is picked. Every policy that files stock pays it within 1% of every other, which
is exactly what makes it a floor and why the results are quoted *above* it.

So "some cost is unavoidable" means **unavoidable by the identification decision**, which is the
only thing being compared. It is not a claim that a real warehouse must lose a tenth of its
stock. That figure is a property of the simulated inventory policy; forecasting was left out of
scope because it has no bearing on whether a batch was identified correctly.

One policy does move the floor. **Always segregate** writes off 47% more (18,518 units against
12,551), because dating everything at the earliest expiry any batch could have means much of it
is scrapped before anyone comes to identify it. That is a cost its own decisions cause, so it
is not part of the floor — and it is a large part of why holding everything is expensive rather
than merely unhelpful.

### The two paid lookups

| | Batch registry (£0.30) | Shipment ledger (£0.40) |
|---|---|---|
| Answers | When was this batch released from quality hold, and which customers has it ever been allocated to? | What was physically scanned out of the door? |
| Source | The manufacturing and allocation record | Door scans at dispatch |
| Best used when | You need to rule a batch *out* — it could not have been in that shipment, or was never sent to this customer | The **warehouse system itself** is the suspect source, because door scans are unaffected by a stale replica |
| Code | `services/batch_registry.py` | `services/shipment_ledger.py` |

The registry is what settles the hero case: `B-2291` cleared quality control on 2026-06-28,
*after* the shipment to that customer left on 2026-06-02. It could not have been in the box.

### Where the cost table lives

| | |
|---|---|
| `config/harm.yaml` | **The basis**, not the answers. Unit cost, gross margin, shelf life, analyst hourly rate, review minutes, human error rate, lookup prices. Each entry is tagged `definition`, `derived` or `judged` so you can see which are real judgements |
| `agent/harm.py` | Works every figure the agent uses out of that basis, so changing a wage or a margin updates everything downstream. Also holds the break-even arithmetic behind the sensitivity table |

The file checks its own relationships when it loads, so a bad edit fails immediately rather
than quietly changing how the agent behaves. Editing it and watching the chosen action change
on identical evidence is how `test_agent.py::test_no_default_branch` proves there is no
hardcoded fallback.

---

## 5. Two things the screen deliberately does not do

**The overall figures are not about the case on screen.** Everything under "how the policy does
overall" is a whole-operation average over 600 simulated runs and is *identical whichever case
you are looking at*. It is below a divider and labelled, because placed next to a single return
it would read as a claim about that return.

**Nothing on screen is recomputed for display.** Every number comes from the agent's own result
object via `demo/panels.py`, which imports no Streamlit at all. There is a test asserting the
panel data matches the agent's result field by field, so what is on camera cannot drift from
what the agent actually did.
---

