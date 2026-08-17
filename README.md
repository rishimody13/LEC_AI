# RECONCILE — deciding what a returned carton actually is

**Repo:** `LEC_AI` · **Brief:** [objectives.md](./objectives.md) · **Plan:** [PLAN.md](./PLAN.md)
· **Progress:** [status.md](./status.md) · **Demo guide:** [demo/GUIDE.md](./demo/GUIDE.md)
· **Design options:** [architecture-options.md](./architecture-options.md)
· **Where the model is used:** [llm-integration.md](./llm-integration.md)

```bash
uv sync --extra dev --extra demo
uv run pytest                              # 693 tests
uv run streamlit run demo/app.py           # the screen
uv run python -m harness.sweep 2000        # the agent against cases nobody wrote
uv run python -m harness.counterfactual 600  # the harm, measured
```

---

## 1. The problem

A warehouse gets part of an order back — say 84 tins out of 240. The carton has a label on it:
a batch code, a best-before date, a handwritten note. The warehouse system also has a record of
what it thinks it sent that customer. **Sometimes these disagree.**

Somebody has to decide which to believe, and then decide where the stock goes. If they get the
batch wrong, nothing breaks that day. Months later somebody ships expired baby formula, or the
system runs out of stock without warning.

That delay is the whole problem. **The mistake is invisible at the moment you make it**, so
nothing in the ordinary run of the warehouse ever tells you it happened.

### What this is

An agent that keeps a probability for each possible answer and takes whichever action costs
least in expected pounds. It has four:

| Action | What it does |
|---|---|
| **Commit** | File the stock to a shelf under a named batch |
| **Gather** | Pay a small fee to look up an external record |
| **Segregate** | Hold it back under the earliest expiry it could possibly have |
| **Escalate** | Hand it to a person |

Nothing is hardcoded. There is no `else` branch: every action, escalation included, is priced
and compared, and the cheapest wins.

### Objectives

| # | Requirement | Where it is met |
|---|---|---|
| R1 | Two decisions in sequence, the second using the first | `agent/loop.py`; both appear in every trace |
| R2 | Real alternatives chosen at runtime, not a script | `test_agent.py::test_no_default_branch` — editing the cost file flips the action on identical evidence |
| R3 | Two independently failing components | `services/label_reader.py`, `services/wms_client.py`, each with its own fault switches |
| R4 | Work out which source is broken, no default | Source failure is a latent variable inside the likelihood, not a filter |
| R5 | Unreadable labels *and* readable-but-contradicted ones | Cases S2/S3 and S4/S8 |
| R6 | Trust, pay to check, or escalate | All four actions win somewhere |
| R7 | Assign stock without causing drift | `ledger/` — append-only, reversible, drift measured against truth |
| R8 | Prove the harm, measured | 600 paired 540-day simulations: **24% fewer expired units** than trusting the label |
| R9 | Working code | 693 tests, ruff and mypy strict clean, runs offline |
| R10 | A case where the obvious answer is wrong | S4, and the demo says so on screen |

---

## 2. End-to-end architecture

```mermaid
flowchart TB
    subgraph W["world/ — ground truth (walled off)"]
        GEN["generators.py<br/>batches, bins, customers, shipments"]
        LAB["labels.py<br/>draws real PNGs, then damages them"]
    end

    subgraph S["services/ — evidence, each able to fail alone"]
        DB[("SQLite<br/>db.py")]
        LR["label_reader.py<br/>vision reading → validated evidence"]
        WMS["wms_client.py<br/>shipment records"]
        REG["batch_registry.py<br/>paid: allocation + QA release"]
        SL["shipment_ledger.py<br/>paid: what actually left"]
    end

    subgraph A["agent/ — cannot import world/"]
        CAND["candidates.py<br/>what it might be"]
        BEL["belief.py<br/>Bayes in log space"]
        CON["constraints.py<br/>physical impossibilities"]
        VOI["voi.py<br/>is a lookup worth buying?"]
        POL["policy.py<br/>cheapest action wins"]
        LOOP["loop.py<br/>the two decisions"]
        TR["trace.py<br/>decision log"]
    end

    subgraph L["ledger/ — the stock record"]
        LED["ledger.py<br/>append-only movements"]
        POST["posting.py<br/>decision → movements"]
        DRIFT["drift.py<br/>belief vs truth"]
    end

    subgraph D["downstream/ — 18 months of consequences"]
        DEM["demand.py"]
        PICK["picking.py<br/>first-expired-first-out"]
        TRUTH["truth.py<br/>what is really on the shelf"]
        SIM["simulate.py"]
    end

    GEN --> DB
    LAB --> LR
    DB --> WMS & REG & SL
    LR & WMS --> CAND --> BEL
    REG & SL --> CON --> BEL
    BEL --> VOI --> POL --> LOOP --> TR
    LOOP --> POST --> LED
    LED --> DRIFT
    GEN -.ground truth.-> DRIFT
    LED --> PICK --> SIM
    DEM --> SIM
    TRUTH -.what really ships.-> SIM

    style W fill:#fff4e6
    style A fill:#e8f4ff
    style L fill:#eefbe8
    style D fill:#f6e8ff
```

**The wall matters more than anything else here.** `agent/` cannot import `world/`, and neither
can `ledger/`. Both are enforced by a test that parses the imports. Without it, every harm
number in this repo would be unfalsifiable — the agent could read the answer, and a drift of
zero would mean nothing because both sides of the comparison would come from the same place.
`ledger/drift.py` and `downstream/` are the only places the two sides meet, which is exactly
why they are separate files.

### 2.1 The world

`world/generators.py` builds one fixed, hand-checkable warehouse: 5 batches of one product,
8 bins including a goods-in bin, a hold area and a quarantine area, 4 customers (one of which
repacks goods — that is the one whose labels lie), 8 shipments and 8 return events.

Those 8 returns give **12 cases**: S1–S8 use one each, and the four `X-` cases replay `RET-S1`
and `RET-S4` with a different service knocked out. So a source failure is tested against a
return whose correct answer is already known from the undamaged run, which is what makes "the
agent still resolved it" mean something.

`world/labels.py` draws each carton label as a **real PNG and then damages it** — glare, blur,
smear, a torn corner, ink bleed. The damage is genuine image damage, not a flag on a data
structure, so whatever reads it has to cope with characters that are actually missing.

Batch codes carry a **GS1-style check digit**: `B-2288` prints as `B-2288-0`, where the last
digit is computed from the others. That is what lets the agent tell "I misread a character"
apart from "this label genuinely says something else". Changing any single body digit always
breaks the check digit — verified exhaustively — which is why case S6 is a physical tear rather
than a misread.

### 2.2 How a decision is made

```mermaid
sequenceDiagram
    participant R as Return arrives
    participant L as Label reader
    participant W as WMS records
    participant C as Candidates
    participant B as Belief
    participant V as Value of information
    participant P as Policy
    participant K as Ledger

    R->>L: read the carton
    L-->>C: code fragment, confidence, check digit, damage
    R->>W: what did we send this customer?
    W-->>C: shipments (possibly stale, conflicting, or timed out)
    Note over C: candidates = records + code fragment<br/>+ batch codes named in the note<br/>+ a catch-all for everything else
    C->>B: prior from stock on hand
    B->>B: × P(label | candidate)
    B->>B: × P(records | candidate)

    P->>V: what would a lookup be worth?
    V-->>P: expected cost after each subset of lookups
    P->>P: price commit / segregate / escalate / gather
    Note over P: DECISION 1 — who to believe
    alt gather wins
        P->>K: buy registry and/or shipment ledger
        K-->>B: constraints + dispatch evidence
    else escalate wins
        P-->>R: to a person
    end
    P->>P: re-price every terminal action
    Note over P: DECISION 2 — where it goes
    P->>K: post the movements
```

**Source failure is inside the sum, not a filter in front of it.** The likelihood marginalises
over what the source might be doing:

```
P(evidence | candidate) = Σ over source states  P(state | symptoms) × P(evidence | candidate, state)
```

A label can be in one of three states — read correctly, misread, or *genuinely correct but on
the wrong box* (a reused outer carton). That third state is what lets the agent say "this label
is perfectly legible, and I still do not believe it", which is the hero case.

### 2.3 Where the model is used

Two calls, both `claude-sonnet-5`, and **neither runs when the agent runs**:

| Call site | Input | What it returns |
|---|---|---|
| `services/label_reader.py` | the carton photo | which characters are legible, whether the code is complete, a confidence, the best-before date, the physical condition |
| `agent/notes.py` | the handwritten condition note | print dates, **any lot code written out in prose**, and three flags: repacked, mixed pallet, off-site origin |

Both are reached only by `services/record_readings.py`, an offline recorder. Everything else —
tests, demo, sweeps, simulation — replays the recordings in `tests/cassettes/`, keyed by the
sha256 of the image bytes or the note text. Change an image and its recording no longer
matches, so the reader raises rather than quietly returning a stale answer. A test replaces
`anthropic.Anthropic` with a class that raises on construction and runs every case, so "no
model at runtime" is verified rather than asserted. **No API key is stored anywhere.**

The split is **the model perceives and interprets; plain code decides**, and it is enforced
structurally rather than by discipline: `LabelReading` and `NoteFacts` have no field in which a
judgement could be expressed. There is nowhere for the model to say which batch it thinks the
stock is, or what should be done. Even a drifting prompt could not carry the answer.

**What the model contributes that a database query cannot.** A warehouse note saying
*"cross-docked from the Halden depot, inner cases stamped B-2296"* is prose. No `SELECT` will
ever find that code. The model reads it out; `agent/candidates.py` checks it against the real
batch list before it becomes a candidate, so an invented code cannot win; and
`agent/notes.py::note_code_likelihood` then treats it as evidence for that batch at 16:1.

That path is measured, not assumed. Over 600 generated cases that contained a lot code in the
note, removing the model's extraction changes the decision on a large share of them, and the
agent gets more of them right with it than without. `agent/candidates.py` itself contains no
model call — it consumes `NoteFacts` — which is the point of the split, but it does mean the
contribution has to be measured end to end rather than inferred from the code.

### 2.4 The stock ledger

Append-only, enforced by SQLite triggers rather than good intentions: `UPDATE` and `DELETE`
abort. Two ideas do the work:

- **Movements are transfers, not adjustments.** Every row moves a positive quantity from one
  place to another, and places outside the warehouse (`@customer`, `@dispatch`, `@scrap`) are
  named. "Units in equals units out" is then true by construction.
- **A position is a lot at a location**, where a lot is what we *believe* the units are.
  Changing our mind is therefore a move between positions — an event in the log with a decision
  attached, not a silent overwrite.

Every return produces exactly two rows: a receipt into goods-in as unidentified stock, then the
placement. **Escalations post too** — stock a person is looking at is real stock in a real
place.

---

## 3. The simulation

The harm argument cannot rest on an anecdote, so six policies are run through the same
eighteen months, on the same warehouses, with the same demand and the same returns.

```mermaid
flowchart LR
    subgraph DAY["each simulated day"]
        D1["1 · deliveries land"]
        D2["2 · reviews finish<br/>a person, right 99% of the time"]
        D3["3 · returns arrive<br/>the policy decides"]
        D4["4 · orders ship<br/>first-expired-first-out"]
        D5["5 · write off<br/>what the record says has expired"]
        D6["6 · reorder"]
        D1 --> D2 --> D3 --> D4 --> D5 --> D6
    end
    D3 -->|belief| LED[("stock ledger")]
    D3 -->|reality| TT[("truth tracker")]
    LED -->|ranks by recorded expiry| D4
    TT -->|decides what really ships| OUT["expired units shipped"]

    style OUT fill:#ffe6e6
```

**The mechanism is in step 4.** Picking ranks stock by the expiry *the record says*. What
physically leaves is whatever is actually in that location. Stock recorded as lasting longer
than it really does sinks to the back of the queue, waits, and ships after it has gone off.
Nothing malfunctions at any point — the picker is doing its job correctly on a wrong number.

Two things are never picked, and the cost model depends on both: **stock with no recorded
expiry** (first-expired-first-out cannot rank what it cannot date) and **non-active bins**.

### 3.1 Running it

```bash
uv run python -m harness.counterfactual 600
```

The comparison is **paired**: for a given seed every policy gets the same warehouse, the same
demand, the same returns and the same faults, so the only difference is what was decided. The
variation between seeds is far larger than the difference between policies, so an unpaired
comparison would bury the effect. There is a test asserting the pairing holds, because it broke
once and the results still looked plausible.

Confidence intervals are bootstrapped over seeds rather than assumed normal — the per-seed harm
is heavily skewed, and a normal interval would be wrong in the direction that flatters the
result.

### 3.2 Results — 600 seeds, 540 days each

| policy | expired units shipped | £ per run | £ above the floor |
|---|---:|---:|---:|
| **agent** | **284.9** | 152,143 | **15,227** |
| trust the label | 374.3 | 155,671 | 18,755 |
| trust the records | 1,756.8 | 221,091 | 84,175 |
| always escalate | 782.9 | 176,652 | 39,736 |
| always segregate | 128.5 | 211,783 | 74,867 |
| oracle (knows the answer) | 0.0 | 136,916 | 0 |

Paired against each alternative, 95% bootstrap intervals. Negative means the agent is better:

| against | expired units | total £ |
|---|---|---|
| trust the label | **−89.4 [−129.5, −50.6]** | **−3,528 [−5,193, −1,902]** |
| trust the records | −1,472 [−1,681, −1,270] | −68,948 [−76,422, −61,735] |
| always escalate | −498 [−542, −457] | −24,508 [−26,573, −22,571] |
| always segregate | +156 [+119, +194] | −59,640 [−63,304, −55,927] |

**Read the third column, not the second.** The absolute £ figure is ~94% stock written off at
its recorded best-before, and that comes from the **replenishment rule, not from any decision
about a return**: a fixed order-up-to level with no forecasting leaves long-dated stock sitting
behind shorter-dated deliveries in the queue until some of it ages out. Every policy that files
stock pays it within 1%, which is what makes it a floor. Against the part decisions actually
control, the agent is 19% better than trusting the label.

This is why **the oracle costs £136,916 rather than nothing.** It knows the answer, so it ships
zero expired units, escalates nothing and buys no lookups — its entire cost is that same
write-off. "Some cost is unavoidable" means unavoidable *by the identification decision*, which
is the only thing being compared here. It is not a claim that a real warehouse must lose 10% of
its stock; that figure is a property of the simulated inventory policy, and forecasting was
deliberately left out of scope because it has no bearing on whether a batch was identified
correctly.

The one policy that does move the floor is **always segregate**, which writes off 47% more
(18,518 units against 12,551) because holding everything under the earliest expiry any batch
could have means much of it is scrapped before anyone gets round to identifying it. That is a
cost its decisions genuinely cause, and a test keeps the distinction honest.

Two results worth reading carefully:

- **Escalating everything is not safe.** It ships 783 expired units against the agent's 285,
  because a 1% human error rate applied to *every* return beats a larger rate applied only to
  the hard ones. Had review been modelled as a free correct answer, this policy would have been
  unbeatable and the comparison rigged.
- **Segregating everything is safe and useless.** Fewest expired units of any runnable policy,
  and £59,125 more expensive, because held stock is never on a pick face when it is needed.
  Quoting the expiry number alone would make it look like the winner.

### 3.3 Six months was not long enough

The plan assumed a 180-day horizon. That was the biggest flagged risk and it was real:

| horizon | 180d | 270d | 365d | 540d | 730d |
|---|---:|---:|---:|---:|---:|
| expired units, same 8 seeds | 644 | 3,129 | 1,772 | 4,730 | 3,729 |

At 180 days **six of the eight seeds report zero**. A six-month run would have concluded there
was no difference between policies worth having. Being slow is the entire point of this
failure, so the measurement has to outlast it. The horizon is 540 days and a test keeps it
there.

---

## 4. Testing

693 tests. The important distinction is between the two kinds:

| | recorded cases | generated cases |
|---|---|---|
| How many | 12, written by hand | unlimited, from a seed |
| Warehouse, database, services | real | real |
| Label image | rendered PNG, read by a vision model, recorded | constructed, put through the same validation |
| What they prove | perception works; the agent handles what it was shown | the agent handles cases nobody wrote |

**Every bug found in this project hid behind the same thing**: cases written by hand and added
only once the code could already handle them. The generative harness (`harness/`) exists
because of that. It builds fresh worlds from seeds and asserts *properties*, never expected
answers, in two worlds:

- **Calibrated** — faults occur at the rates `config/reliability.yaml` claims. A failure here
  is a reasoning failure.
- **Miscalibrated** — every fault equally likely. Failures there mean the beliefs are wrong,
  which is a different thing, so only the properties that must hold regardless are checked.

```bash
uv run python -m harness.sweep 2000                 # calibrated
uv run python -m harness.sweep 2000 --miscalibrated
uv run python -m harness.calibration                # is it as sure as it should be?
uv run python -m harness.calibration --sweep        # the evidence weight, measured
```

---

## 5. Honest limits

- **The reliability numbers are hand-set.** They are in `config/reliability.yaml` and can be
  argued with, but they are not measured, and they are load-bearing for the hero case. The
  measured cost of this is the gap between 0.20% dangerous outcomes in the calibrated world and
  2.05% in the miscalibrated one.
- **The agent is overconfident by about a factor of two.** It claims a 1.8% error rate and has
  a 3.5% one. The evidence weight improves its *decisions* without fixing this; the fix needs a
  model of which sources share what information, not one scalar.
- **It is risk-neutral.** A certain £4,000 loss and a 1-in-100 chance of £400,000 are the same
  to it. A food business would not agree.
- **Evidence nobody anticipated is ignored.** A temperature log has no likelihood function, so
  it has no effect.
- **Its value has an upper bound in return size.** Above roughly a hundred units on the hero
  case, a person costs less than the risk left after a lookup, so the agent stops buying
  information. Where that point sits is set by `human_error_rate` — another hand-set figure.

---

## 6. Edge cases, and what happens

Everything below is exercised by a test. Most of them were found the hard way — by the
generative sweep, or by probing — rather than anticipated.

### The evidence is missing or broken

| Case | What happens |
|---|---|
| **Label unreadable** — water damage, heavy glare | No code, so the label likelihood is flat across candidates. It contributes nothing rather than contributing noise |
| **Label reader itself is down** | Same, but the failure is recorded as a symptom so the reliability model knows which bucket applies |
| **Check digit fails** | The characters do not add up, so *something* was misread. The confusion table decides which real codes could have garbled into what was read |
| **Code is incomplete** — torn corner | The fragment is used as a prefix: every known batch whose code starts with it becomes a candidate. Without this the true answer would often never be on the list |
| **Label names a code that is not a real batch** | Recorded as `rejected` and its weight goes to the catch-all. An invented or misread code cannot win |
| **Warehouse records time out** | No records *and no batch list*. The agent still has the label, and a legible code becomes an unverified candidate rather than being thrown away |
| **Records are stale** — replica behind | Recent shipments are missing by definition, so the chance the answer is not on the list rises. Ignoring this once had the agent file an impostor at 99.96% |
| **Records contradict themselves** | Treated as a corrupted-source symptom, which is a different reliability bucket from a slow one |
| **A paid lookup is unavailable** | Priced as buying nothing. The agent does not pay for a source that cannot answer |
| **No evidence at all survives** | The candidate list is just the catch-all, at 100%. It still weighs five options and holds the stock (£5.12) rather than escalating (£29.50). With no batch list there is no date it can stand behind, so the hold is left *undated* — which means it cannot be picked at all, and carries no expiry risk. Being ignorant safely |

### The stock is not what the paperwork implies

| Case | What happens |
|---|---|
| **A reused outer box** — the label is genuine but on the wrong contents | This is the hero case. "Wrong label" is one of three states the label channel can be in, so a perfectly legible code can still be disbelieved |
| **The customer repacks** | Their labels are wrong far more often, *and* "you cannot return more than we sent you" stops being a rule — they merge stock across deliveries. Applying it anyway ruled out the true batch precisely for the customers who make cases hard |
| **Cross-docked or transferred stock** | Never appears in our shipment records at all. The record-completeness rules are switched off, and the catch-all's share of the prior rises |
| **The answer appears only in prose** | A note saying *"inner cases stamped B-2296"* is invisible to a `SELECT`. The model reads it out, code checks the batch is real, and it becomes a candidate with evidence behind it |
| **A batch that could not have been in that shipment** | Cleared quality control after the lorry left, or was never allocated to that customer. Ruled out — but never to exactly zero, because the registry can be wrong too |

### Degenerate inputs

| Case | What happens |
|---|---|
| **Quantity of zero or less** | Refused at the intake boundary. A negative quantity flips the sign of every harm term, so the *most* damaging action becomes the cheapest and the agent picks it confidently. At −5 units the hero case filed the stock as the label claimed |
| **One unit** | Decided normally, and it will sometimes file the wrong batch — an £8.53 review is not worth spending on a single £11.40 unit. It still never records an expiry *later* than the truth |
| **Thousands of units** | Also decided normally. Above roughly a hundred units on the hero case it stops paying for lookups, because the risk left over after one still exceeds what a person costs |
| **A note naming a batch that does not exist** | Rejected, weight to the catch-all. Same treatment as an invented label code |
| **Evidence that explains nothing** — every candidate near zero | Fifty rounds of it leaves a valid distribution. Arithmetic is in log space with a floor, so nothing underflows to `nan` |
| **The batch list is empty** | Treated as *no information*, not as "no batch matches". Reading an empty list as a rejection suppressed the reused-box explanation fiftyfold and left the agent 99.996% sure of a single unverified reading |

### Stock record and simulation

| Case | What happens |
|---|---|
| **A decision that turns out wrong** | The row stays in the log and a new one puts the stock back. History is never rewritten; `UPDATE` and `DELETE` are blocked by database triggers |
| **Stock held with no expiry date** | Cannot be picked at all — first-expired-first-out cannot rank what it cannot date — so it carries no expiry risk. The picker really does skip it, and a test says so |
| **Two returns held at the same position** | They pool. A pick is split across what is really there in proportion, not in name order, which would quietly ship one batch before another |
| **A hold written off before anyone reviews it** | Handled rather than crashing. It is a real cost of dating stock conservatively, and it is why holding everything is expensive |
| **An escalation** | Still posted to the ledger. Stock a person is looking at is real stock in a real place, and a person resolves it after three days — right 99% of the time, not always |

---

## 7. Key design decisions

**Keep a distribution, not a best guess.** The agent's output is a probability for every
candidate that sums to one, including a catch-all for "something nobody listed". A single best
guess cannot express "the label is legible and I still do not believe it", which is the entire
problem.

**Every action is priced; the cheapest wins.** There is no `else` branch. Escalating to a human
sits in the same costed list as committing, and wins when it is genuinely cheapest. Editing
`config/harm.yaml` flips the chosen action on identical evidence, which is how the absence of a
hardcoded fallback is proved rather than asserted.

**Source failure is inside the likelihood, not a filter in front of it.** The agent marginalises
over what each source might be doing rather than deciding first whether to trust it. That is
what lets a perfectly legible label be disbelieved.

**The model perceives; code decides — enforced by the type, not by discipline.** `LabelReading`
and `NoteFacts` have no field in which a judgement could be expressed. There is nowhere for the
model to name a batch or an action, so even a drifting prompt could not carry the answer.

**No model call at runtime.** Readings are recorded once, keyed by content hash, and replayed.
The demo runs offline with no key, results are reproducible, and a test replaces
`anthropic.Anthropic` with a class that raises to prove it. The cost is that a re-record is
needed whenever an image changes — deliberate, because a demo that depends on a network call is
a demo that fails while being filmed.

**Ground truth is walled off, and the wall is tested.** Neither `agent/` nor `ledger/` can
import `world/`. Without this every harm number would be unfalsifiable.

**Nothing is ever driven to exactly zero.** Nothing recovers from zero, and the sources that
rule candidates out can themselves be wrong. A broken constraint collapses a candidate to the
chance that source is mistaken, not to nothing.

**Stock movements are transfers, not adjustments**, with places outside the warehouse named
explicitly. "Units in equals units out" is then true by construction rather than by convention.

**Costs are derived from a basis, not typed in.** `config/harm.yaml` holds unit cost, margin,
wage and minutes; `agent/harm.py` derives every figure from them, so changing a wage updates
everything downstream and cannot leave two numbers disagreeing.

**Report break-even, not a sensitivity sweep.** Expected cost is linear in each cost parameter,
so the exact value at which a decision flips can be solved for directly. That is more useful
than sampling around a guess, and it is what the demo's sensitivity table shows.

**Test properties on cases nobody wrote.** Every bug in this project hid behind hand-written
cases added once the code could already handle them. The generative harness asserts what must
never happen, in a world calibrated to the agent's beliefs and one deliberately not.

**Discount evidence for not being independent.** Multiplying likelihoods assumes the sources
are independent given the batch, and they are not — a reused box carries a genuine label of a
batch that genuinely went to that customer. Constraints are exempt, because they do not
corroborate anything and already state their own error rate.

**Prove the harm with a paired comparison against real rivals.** Same warehouse, same demand,
same returns for every policy, over eighteen months, with bootstrapped intervals. "Trust the
label" is what warehouses actually do and is right most of the time; beating a straw man would
prove nothing.

---

## 8. Layout

```
world/       ground truth: warehouse, batches, label images   (agent/ must not import)
services/    evidence sources, each able to fail on its own
agent/       candidates, belief, constraints, value of information, policy, loop, trace
ledger/      append-only stock movements, and drift against truth
downstream/  demand, first-expired-first-out picking, the 540-day simulation
harness/     generated cases, properties, sweeps, policies, the counterfactual
demo/        the screen (panels.py holds the arithmetic, app.py only renders)
config/      cost basis, reliability counts, the 12 recorded cases
artifacts/   label images, decision traces, committed harm figures
```
