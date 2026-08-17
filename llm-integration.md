# Where the model is used, and where it is not

**Repo:** `LEC_AI` · **Progress:** [status.md](./status.md)

## The short version

There are two calls to Claude in this project. **Neither of them runs when the agent runs.**
They are used once, offline, to record what the model saw; the agent then works entirely from
those recordings. No API key is stored anywhere in the repository.

---

## 1. Where the calls are

| # | File | Model | Input | Output |
|---|---|---|---|---|
| 1 | `services/label_reader.py` | `claude-sonnet-5` | A carton label photo (PNG, base64) | Which characters of the batch code are legible, whether the code is complete, a confidence figure, the best-before date, and the physical condition |
| 2 | `agent/notes.py` | `claude-sonnet-5` | The handwritten condition note (text) | Print dates, any lot codes written in prose, and three flags: repacked, mixed pallet, off-site origin |

Both use the Anthropic Python SDK's `messages.parse()` with a Pydantic model as the output
format, so the reply is validated against a schema rather than parsed out of prose.

Model choice is a constructor argument. `services/label_reader.py` exposes it as
`DEFAULT_VISION_MODEL`, so switching to `claude-haiku-4-5` is a one-line change. Sonnet is
the default because the hard part of reading a damaged label is restraint — not guessing at
characters that are not there — and a weaker model is more likely to fill in a plausible code.

## 2. What the model is *not* allowed to do

The split is deliberate: **the model perceives and interprets; plain code decides.**

| Job | Who does it |
|---|---|
| Read characters off a photo | Model |
| Extract facts from free text | Model |
| Decide whether a code is well formed | Code (`common/coding.py`) |
| Check the check digit | Code |
| Decide which warning signs apply | Code (`services/label_reader.py::to_evidence`) |
| Work out how likely each candidate is | Code (`agent/belief.py`) |
| Decide whether to buy a lookup | Code (`agent/voi.py`) |
| Choose an action | Code (`agent/policy.py`) |

This is enforced structurally, not by discipline. The `LabelReading` and `NoteFacts` schemas
have **no field in which a judgement could be expressed** — there is nowhere for the model to
say which batch it thinks the stock is, or what should be done. Even if a prompt drifted, the
type would not carry the answer.

The three things the model contributes that a database query cannot:

1. Reading characters off a damaged photo.
2. Pulling a lot code out of a sentence — `"inner cases stamped B-2296"` is invisible to a
   `SELECT`. This is what case S7 is built around.
3. Recognising that a note describes a cross-dock or a mixed pallet, which tells the agent
   the shipment records are not going to cover this stock.

**Point 2 was dead weight until it was measured, twice over.** The model extracted the code and
`agent/candidates.py` turned it into a candidate, but nothing treated that code as *evidence*
for the batch it named — `note_likelihood` only ever looked at print dates — and the one
function that might have was gated behind a paid lookup, which S7 never buys. The candidate
appeared, carried no support, and changed no decision on any of the twelve cases or on any of
32 generated cases that had a lot code in the note.

It is now split into `note_code_likelihood`, applied immediately alongside the records and the
label, and `note_date_likelihood`, which still waits for the registry. On S7 the true batch,
named nowhere but in prose, goes from 0.20 to **0.78**. Across generated cases the extraction
now changes 13 of 32 outcomes and gets 10 right that would otherwise be wrong.

The lesson is about testing rather than about the model. There were already tests that the code
path ran, that the candidate appeared, and that an invented code was rejected. All passed the
whole time. None asked whether it changed a decision.

Anything the model proposes is checked before use. A batch code found in prose only becomes
a candidate if it matches a batch that actually exists; otherwise it is recorded as rejected
and its weight goes to the catch-all. An invented code cannot win.

Note that `agent/candidates.py` contains **no model call at all** — it consumes the `NoteFacts`
the reader produced. That is exactly the split working as intended, but it has a consequence
worth stating: you cannot tell from reading `candidates.py` whether the model is contributing
anything. It has to be measured end to end, and when it was not, it was not contributing.

## 3. How it runs offline

Both readers sit behind an interface with three implementations:

| Implementation | Used by | Behaviour |
|---|---|---|
| `CassetteLabelReader` / `CassetteNoteReader` | Everything, always | Replays a recorded reading |
| `UnavailableLabelReader` | The reader-down test case | Returns no evidence |
| `ClaudeLabelReader` / `ClaudeNoteReader` | `services/record_readings.py` only | Calls the API |

Recordings live in `tests/cassettes/` and are keyed by **content hash** — the sha256 of the
image bytes, or of the note text. Change an image and its recording no longer matches, so the
reader raises rather than quietly returning a stale answer.

To re-record after changing the images:

```bash
uv run python -m services.record_readings     # needs an API key and network
```

Everything else — the tests, the demo, the generative sweep — needs neither.

**This is verified, not asserted.** The check replaces `anthropic.Anthropic` with a class that
raises on construction, then runs every case:

```python
import anthropic


class Boom:
    def __init__(self, *a, **k):
        raise AssertionError("LLM call at runtime")


anthropic.Anthropic = Boom
# ... run all cases: they complete
```

## 4. API keys

**None are stored. Anywhere.** Checked in four places:

| Where | Result |
|---|---|
| Repository files (source, config, markdown, JSON) | Nothing matching a key |
| `.env` files | None exist, and `.gitignore` covers `.env` regardless |
| Git history, all branches | Nothing matching `sk-ant-…` |
| Shell environment | `ANTHROPIC_API_KEY` not set |

The client is constructed with no arguments:

```python
client = anthropic.Anthropic()
```

which makes the SDK resolve credentials from the environment at call time — `ANTHROPIC_API_KEY`,
or an `ant auth login` profile. Nothing is read from a file in the repository, and nothing is
written back. The recordings that *are* committed contain only what the model reported about
each image and note.

## 5. Cost

Two calls per return, only while recording. At the time of writing there are 8 label images
and 8 notes, so a full re-record is 16 calls. The agent itself costs nothing to run.

The generative sweep does not call the model at all — it constructs readings directly and puts
them through the same validation code. Perception is covered separately by the image tests and
the recorded readings. That is a deliberate limit worth stating: **the sweep tests the agent's
reasoning across thousands of cases, not the model's ability to read a label.**

## 6. Honest limits

- **The recordings were produced by a vision model reading the rendered images**, and they are
  a fixed snapshot. If the live model would read an image differently today, the recordings do
  not know that. Re-recording is a one-command job but it needs a key.
- **The note reader is only exercised on 8 notes**, and only one of them contains a lot code.
  The *use* of an extracted code is now measured across hundreds of generated cases, but the
  *extraction* itself — whether the model finds a code that is really there, and refrains from
  inventing one that is not — rests on those 8 recordings.
- **A model that reads a label wrongly and confidently is not detectable here.** The check
  digit catches most corruptions, and the reliability model prices the rest, but a confident
  misread that happens to produce a valid code would pass through. That is the same failure the
  hero case is built around, arriving from a different direction.
