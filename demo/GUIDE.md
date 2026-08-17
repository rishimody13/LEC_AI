# Reading the screen

**Repo:** [README.md](../README.md) · **Progress:** [status.md](../status.md)

```bash
uv sync --extra demo
uv run streamlit run demo/app.py
```

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
│  DECISION 1 & 2          │  WHAT IT COSTS LATER     │
│  every action, priced,   │  filed vs truth, drift,   │
│  the winner, the margin  │  the 600-run simulation   │
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
- **The sensitivity table** below says how far one cost figure would have to move to change the
  answer. If it is empty, no single figure can be moved to a sensible value that would flip it.

Both decisions appear. The second uses the probabilities the first produced, and after a
lookup they are usually very different.

### Bottom right — what it costs later

The immediate consequence: what the stock was filed as, what it really was, and the drift that
leaves in the record. Green means no drift at all. Red means the recorded expiry is wrong, and
by how many days.

Below that, the committed simulation: six policies over 600 runs of 540 days each. The metric
at the bottom — expired units avoided against trusting the label — is the headline result, with
its confidence interval in the tooltip.

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
| 7 | A large return with clean evidence, and it *still* escalates — because at 1,192 units a person costs less than the risk left over |
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

## 4. Filming notes

- Opens on S4 so the film does not start on a menu.
- Everything is precomputed and cached; switching case is instant, and there is no flicker to
  edit around.
- The cost table is dense. Hold it for a full eight seconds and highlight one row at a time.
- The strongest beat available: run S4, then switch to generated seed 418 and show the agent
  getting one *wrong*, live, on a case nobody wrote. An interface that could only display
  successes would be worth nothing.
