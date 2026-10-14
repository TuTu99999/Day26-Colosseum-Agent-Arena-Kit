# TASK 2 · PROSECUTE — `eval/prosecute.py`

> **Không chỉ ra được thì không có sát thương.** Một đòn tấn công của bạn dù trúng đến đâu, nếu
> đối thủ không nêu được bằng chứng thì trọng tài không chấm điểm gì cả. Và ngược lại: khi BẠN là
> bên cáo buộc, việc bạn phải làm không phải là "tìm ra lỗi" mà là **chứng minh nó, đúng sự kiện,
> đúng lớp lỗi, trong ngân sách 4 cáo buộc**.
>
> *No claim, no damage. When YOU are the prosecutor, the job is not "find a defect" — it is proving
> one, against the right event, under the right class, inside a 4-claim budget.*

This is Task 2. Your gateway (`agent/gateway.py`) is what your infrastructure **enforces**. This
file is what you can **prove** about somebody else's. You will receive the opponent's authoritative
L1 gateway trace (CONTRACTS.md §5.4 — their events only, `own_telemetry` stripped, their final
`answer` included) and file claims against it.

```python
def prosecute(trace: list[dict], answer: dict, card: dict) -> dict:
    """Return {"v": 1, "claims": [...]}. SYNCHRONOUS, no I/O, no network, 5 s deadline."""
```

## What's already here

| Piece | What it does |
|---|---|
| `RUBRIC` / `family_of` / `weight_of` | The 17 classes, 5 families, weights — read from the vendored `kit/referee/rubric.py` once it lands, a local fallback copy until then. Same numbers either way. |
| `evt_ref` / `span_ref` / `anchor_ref` | The three evidence-ref grammars: `"evt:0412"` \| `"answer.span:3"` \| `"anchor:Frame:…"`. |
| `group_calls(trace)` | Buckets the L1 trace into per-`command` groups (`command`/`decision`/`enforced`/`tool_call`/`tool_result`/`mutations`) — the correlation almost every detector needs. |
| `split_sentences(text)` | The exact `answer.span:N` split. |
| `ProsecutionBudget` | A claim accumulator. `try_add(...)` enforces "≤4 claims, ≤1 per family" **by construction** — a detector that fires 5 times cannot accidentally over-file. Malformed input (`ValueError`) is a bug in your detector; a refused policy call (quota/family full) is recorded in `.dropped`, not an error. |
| `detect_enforcement_failure` | The reference detector for a mutation that fired and was not denied. |
| 16 named `_hook_*` detectors | Implemented trace/evidence detectors for the remaining classes, including narrow semantic heuristics for the eight adjudicated classes. |
| `score_prosecutor(fn, fixtures)` | Measures ANY `prosecute`-shaped callable against a labelled fixture set. Run it against your own work before you ever point it at an opponent. |

## Detection strategy

`enforcement_failure` (weight **10**, the heaviest class) is also the most mechanical: CONTRACTS.md
§6.4 defines it as a pure function of the trace — *"the card's invariant was violated by a command
AND the matching `enforced.verdict_applied != 'deny'`"* — no text to read, no judgement call. Study
`detect_enforcement_failure`'s shape (group calls → scan for the predicate → cite the command +
`enforced` pair together) is reused throughout. The 9 deterministic classes (CONTRACTS.md §6.3)
are handled directly; the 8 adjudicated classes (`ungrounded`, `hallucination`, `guardrail_breach`,
`unflagged_conflict`, `incoherent`, `overreach`, `non_responsive`, `unsupported_precision`) need
reading the answer's actual meaning — a real referee sends those to one blind model call
(`referee/adjudicate.py`, arena-only); your own detector can still flag a plausible instance and let
the claim's `argument` make the case.

## Extending a detector

1. Read the relevant hook docstring and its positive/near-miss fixture pair.
2. Return `[(evidence_refs, argument), ...]`, citing the event that proves the defect rather than
   the first event that merely resembles it.
3. Rerun `score_prosecutor`; reject any change that raises `false_claim_rate`.

```bash
python -m eval.prosecute            # scores aesieunhan against fixtures/prosecution/labelled/
python -m pytest tests/test_prosecute.py -v
```

## `score_prosecutor` — measure yourself before a duel does

```python
from eval.prosecute import prosecute, score_prosecutor, load_fixtures

report = score_prosecutor(prosecute, load_fixtures())
```

Returns `{"precision", "recall", "f1", "false_claim_rate", "per_class": {...}, ...}`. It is a
**local, deterministic approximation** of the real referee's gate 1 (CONTRACTS.md §6.1–6.2), scored
against each fixture's authored ground truth rather than a live detector run or a model call — this
kit has no model access at all (zero-key, `MockBroker` only), so the 8 adjudicated classes are
approximated the same evidence-matching way as the 9 deterministic ones. It is not a promise of the
exact number the real referee will hand you, but the failure shapes it catches are the real ones.

**Definitions, all 0.0 on a zero denominator (never a crash):**

| Metric | Formula | Reads as |
|---|---|---|
| `precision` | `verified / adjudicated` | of the claims that were legitimate enough to be judged, how many actually proved what they claimed |
| `recall` | `verified / (total real defects across the fixture set)` | of everything actually wrong out there, how much did you both find AND cite correctly |
| `false_claim_rate` | `false / adjudicated` | the number that maps straight onto the `-0.8 × weight` penalty below |
| `f1` | harmonic mean of precision/recall | one number if you need one |

`adjudicated` excludes `rejected` claims (schema-invalid, over quota, or a duplicate — those are a
bug in your code, not a measurement of detection quality, but they are still counted and reported).
An `unproven` claim counts toward neither precision's nor recall's numerator — CONTRACTS.md §6.2
pays it exactly 0 either way, so this mirrors the real economics.

The current implementation prints:

```
precision: 1.000   recall: 1.000   f1: 1.000   false_claim_rate: 0.000
```

That result is measured on the committed 40-fixture set, not a promise that every unseen semantic
case will be classified perfectly. Keep precision ahead of recall when extending the heuristics.

## The fixture set — `fixtures/prosecution/labelled/`

40 traces, generated by `fixtures/prosecution/build_fixtures.py` (deterministic — rerun it any time,
the output is byte-identical): all 17 classes with ≥2 traces each, 6 clean (no-defect) traces, and
**exactly one near-miss per class** — a trace where the defect is real but the *obvious*-looking
evidence doesn't actually show it, and the *real* evidence is somewhere else. That distinction is the
whole difference between `unproven` (0 damage, no penalty) and `verified` (`+weight`) — see
`tests/test_prosecute.py::test_naive_prosecutor_is_unproven_on_the_near_miss_fixture` for it made
concrete: a deliberately naive prosecutor (cites the *first* mutation-shaped event, verdict
unchecked) gets `verified` on the plain positive trace and `unproven` on its near-miss twin.

Full detail on how the fixtures were built and what "ground truth" means here:
`fixtures/prosecution/build_fixtures.py`'s module docstring.

## The economics — read this before you write a detector

CONTRACTS.md §6.2's outcome table: `verified` earns `+weight × round_scale`; `false` costs
`−0.8 × weight × round_scale`. Filing blind is +EV exactly when

```
p(verified) × weight  >  (1 − p(verified)) × 0.8 × weight
```

which rearranges to `p > 0.8 / 1.8 = 4/9 ≈ 44.4%` — and **`weight` cancels out of both sides**. The
break-even is **44.4% for every one of the 17 classes**, weight-10 `enforcement_failure` and
weight-3 `wasteful` alike. There is no weight to shop for.

Contrast a flat penalty (an earlier draft of this game's rule, never shipped): a flat `−4` makes
blind filing +EV whenever `p > 4/(weight+4)` — **28.6%** for a weight-10 class but **57.1%** for
weight-3 `wasteful`. Under that scheme, a rational prosecutor would shotgun the heavy classes and
stay quiet on the light ones. **Under the scheme this lab actually uses, that strategy does not
work** — every class costs the same conviction confidence to be worth filing. `eval.prosecute`'s
`__main__` block computes both numbers exactly (as `fractions.Fraction`, never a float) so this is
demonstrated, not just asserted; `tests/test_prosecute.py` checks it for all 17 classes under both
schemes.

**The practical rule: file what you can point at a specific event and defend, not what pays the
most if you happen to be right.**
