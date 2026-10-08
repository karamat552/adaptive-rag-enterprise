"""The battery's mix_declaration — reconciled state, pinned deliberately.

RECONCILED 2026-10-07 (deliberate): the coverage expansion
(GrossProfit/OperatingIncomeLoss/ResearchAndDevelopmentExpense fetched
from SEC companyfacts for the existing three companies; 16 facts, was 8)
re-classed B01/B04/B05, and the declaration was reconciled to the actual
classes in the same commit — with the story recorded in the battery's
invariants.

History: the declaration was registered as 14/8 (vs the then-actual 9/9)
and its own per-class counts summed to 46 != 42 — never updated after
registration (benign; the git rewind left no earlier revision to diff, so
nobody asserted otherwise). The red pins below now pin the RECONCILED
state so any future out-of-band edit of the battery's mix turns them RED:
reconciling must be a deliberate act that also updates this test, never a
quiet edit.

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
# THE PINS: these assert the RECONCILED state. An out-of-band edit of the
# battery's mix turns them red — that is the design, not a failure.
# ---------------------------------------------------------------------------
def test_reconciled_exact_matches_actual():
    """in_coverage_exact 12 == actual 12 (was declared 14 vs actual 9). If
    the battery's mix changes again outside a deliberate reconciliation,
    this goes red: update it consciously, note WHY in the battery's
    invariants."""
    d, a = _declared(), _actual()
    assert d["in_coverage_exact"] == a["in_coverage_exact"], (
        f"declared={d['in_coverage_exact']} actual={a['in_coverage_exact']} "
        f"— an out-of-band edit; reconcile deliberately and record why")


def test_reconciled_alias_matches_actual():
    """in_coverage_alias_phrasing 9 == actual 9 (was declared 8 vs actual 9)."""
    d, a = _declared(), _actual()
    assert d["in_coverage_alias_phrasing"] == a["in_coverage_alias_phrasing"], (
        f"declared={d['in_coverage_alias_phrasing']} "
        f"actual={a['in_coverage_alias_phrasing']} — reconcile deliberately")


def test_reconciled_declaration_sums_to_total():
    """The internal inconsistency is dead: the declaration's per-class
    counts now sum to exactly its total (was 46 != 42)."""
    d = _declared()
    total = (_spec().get("mix_declaration") or {}).get("total")
    assert sum(d.values()) == total == len(_spec()["questions"]), (
        f"sum={sum(d.values())} total={total} — the declaration was "
        f"edited out-of-band; reconcile deliberately")


# ---------------------------------------------------------------------------
# The invariants that must HOLD through any reconciliation
# ---------------------------------------------------------------------------
def test_total_matches_questions():
    """The declaration's total must always equal the question count — the
    one number that survives any relabelling."""
    d = _spec().get("mix_declaration") or {}
    assert d.get("total") == len(_spec()["questions"]) == 42


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
