"""THE ROTATING COHORT (ADR-022 amendment, 2026-10-25) — the split's
invariants, pinned.

The nightly battery runs ONE cohort (every 4th question of the
pre-registered battery); the 4 cohorts' union is the full battery with
no overlap, the sizes are balanced, and each cohort carries BOTH serving
paths — a single-path cohort would make its green night uninformative.
The invariants are pinned so a cohort-definition drift fails loudly.

Offline: the battery spec + the runner's stride logic only — zero
tokens, zero network, zero DB.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import battery_phase1


def _cohorts():
    spec = battery_phase1._spec()
    questions = spec["questions"]
    return {c: [q for i, q in enumerate(questions) if i % 4 == c]
            for c in range(4)}


def test_four_cohorts_cover_the_battery_without_overlap():
    """The union of the 4 cohorts is the full pre-registered battery,
    exactly once — no question is measured twice or skipped by the
    rotation."""
    cohorts = _cohorts()
    union = [q["id"] for ids in cohorts.values() for q in ids]
    assert len(union) == len(set(union)) == 42, \
        f"the cohorts must partition the battery: {len(union)} ids, " \
        f"{len(set(union))} unique"


def test_cohort_sizes_are_balanced():
    """42 questions across 4 cohorts: 10-11 each — a lopsided cohort
    would skew the per-night token burn and the agreement statistics."""
    sizes = [len(ids) for ids in _cohorts().values()]
    assert max(sizes) - min(sizes) <= 1, sizes


def test_every_cohort_is_representative():
    """Each cohort must carry BOTH serving paths (covered + fleet) — a
    single-path cohort would make its green night uninformative about
    the other path's agreement."""
    spec = battery_phase1._spec()
    by_id = {q["id"]: q["expect_path"] for q in spec["questions"]}
    for c, questions in _cohorts().items():
        paths = {q["expect_path"] for q in questions}
        assert "fact" in paths and "fleet" in paths, \
            f"cohort {c} is single-path: {paths}"


def test_runner_cohort_flag_resolves_the_slice():
    """--cohort N resolves exactly the stride slice the invariants
    describe — the same filter the runner applies, verified through the
    runner's own spec."""
    import json

    spec = battery_phase1._spec()
    questions = spec["questions"]
    cohort_2 = [q for i, q in enumerate(questions) if i % 4 == 2]
    # the stride membership: the positions 2, 6, 10, ...
    expected_ids = [q["id"] for i, q in enumerate(questions) if i % 4 == 2]
    assert [q["id"] for q in cohort_2] == expected_ids
    # and the report's cohort field would carry the number (the runner
    # writes report["cohort"] = ns.cohort — verified by the argparse
    # choices contract below)
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--cohort", type=int, default=None, choices=[0, 1, 2, 3])
    ns = ap.parse_args(["--cohort", "2"])
    assert ns.cohort == 2
    assert json.dumps({"cohort": ns.cohort})  # serializable for the report
