"""Red-pin the battery's mix_declaration defect — so the fix must be deliberate.

THE DEFECT (independently confirmed twice — 2026-10-05 by the reference
implementation, 2026-10-05/06 by measure_recall.py): the battery's
registered `.mix_declaration` says in_coverage_exact: 14 and
in_coverage_alias_phrasing: 8, but the actual question['class'] counts are
9 and 9. Both sum to 42; every other class matches exactly.

THREE defects, in fact — the third found by this file's own invariant
test (2026-10-07): the declaration's per-class counts sum to 46, not its
claimed total of 42 — internally inconsistent on top of being wrong about
the actual classes.

The battery's registered invariant says questions are FIXED at
registration; no additions, edits, or removals after measurement starts.
Either five questions were re-labelled after registration (invariant
violated) or the declaration was never updated (benign) — with the git
history rewound there is no earlier revision to diff, so nobody asserts
which. This test pins the CURRENT state so that a silent "fix" of the
declaration turns it RED: reconciling the declaration must be a deliberate
act that also updates this test, never a quiet edit.

Offline: reads the battery JSON only; no DB, no network, no keys.
"""
import json
from pathlib import Path

BATTERY = Path(__file__).resolve().parent.parent / "tests" / \
    "battery_phase1_preregistered.json"


def _spec() -> dict:
    return json.loads(BATTERY.read_text(encoding="utf-8"))


def _declared() -> dict:
    return {k: v for k, v in (_spec().get("mix_declaration") or {}).items()
            if k != "total"}


def _actual() -> dict:
    out: dict = {}
    for q in _spec()["questions"]:
        out[q["class"]] = out.get(q["class"], 0) + 1
    return out


# ---------------------------------------------------------------------------
# THE RED PIN: these assert the CURRENT mismatched state. Fixing the
# declaration turns them red — that is the design, not a failure.
# ---------------------------------------------------------------------------
def test_red_pin_in_coverage_exact_is_mismatched():
    """declared=14 vs actual=9 — pinned. If you reconcile the declaration,
    this test goes red: update it consciously, note WHY in the battery,
    and say whether five questions were re-labelled."""
    d, a = _declared(), _actual()
    assert d["in_coverage_exact"] == 14 and a["in_coverage_exact"] == 9, (
        f"the pinned mismatch changed: declared={d['in_coverage_exact']} "
        f"actual={a['in_coverage_exact']} — if this was a deliberate "
        f"reconciliation, update this test and record the reason in the "
        f"battery's invariants; if not, the battery was edited outside "
        f"registration")


def test_red_pin_alias_phrasing_is_mismatched():
    """declared=8 vs actual=9 — pinned; same contract as the exact pin."""
    d, a = _declared(), _actual()
    assert d["in_coverage_alias_phrasing"] == 8 and a["in_coverage_alias_phrasing"] == 9, (
        f"the pinned mismatch changed: declared={d['in_coverage_alias_phrasing']} "
        f"actual={a['in_coverage_alias_phrasing']} — deliberate? record it.")


# ---------------------------------------------------------------------------
# The invariants that must HOLD through any reconciliation
# ---------------------------------------------------------------------------
def test_total_matches_questions():
    """The declaration's total must always equal the question count — the
    one number that survives any relabelling."""
    d = _spec().get("mix_declaration") or {}
    assert d.get("total") == len(_spec()["questions"]) == 42


def test_red_pin_declaration_is_internally_inconsistent():
    """THIRD defect (found by this test's own invariant, 2026-10-07): the
    declaration's per-class counts sum to 46, not its claimed total of 42
    (14+8+4+8+6+6=46). The declaration is internally inconsistent ON TOP of
    being wrong about the actual classes. Pinned like the others: a
    deliberate reconciliation turns this red — update it consciously."""
    d = _declared()
    total = (_spec().get("mix_declaration") or {}).get("total")
    assert sum(d.values()) == 46 and total == 42, (
        f"the pinned inconsistency changed: sum={sum(d.values())} "
        f"total={total} — if deliberately reconciled, update this test "
        f"and record the reason; if not, the declaration was edited "
        f"outside registration")


def test_every_actual_class_is_declared():
    """Every class the questions actually use must appear in the
    declaration — a fix cannot silently retire a class."""
    d, a = _declared(), _actual()
    missing = set(a) - set(d)
    assert not missing, f"classes used by questions but not declared: {sorted(missing)}"


def test_every_question_has_a_class():
    """The class field is the battery's registered mix — every question
    must carry one."""
    for q in _spec()["questions"]:
        assert q.get("class"), f"{q['id']} has no class"
