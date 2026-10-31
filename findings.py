"""
findings.py
===========
The leadership Overview's findings (PLAN v2 item 6, D7): DETERMINISTIC.

Each finding is a fixed, reviewed SQL query sent through the same guarded path
as any other query (app.run_governed_sql -> sql_guard), and a plain-English
template filled from its rows. No LLM: it costs nothing, cannot misstate a
number, and says the same thing to everyone until the data changes.

A finding that cannot be computed -- the guard refuses, the query errors, the
data is missing -- FAILS SOFT: its card says it is unavailable and why, and
never shows a number.

State comparisons only include states with at least MIN_HOSPITALS reporting
hospitals, and say so: an average over four hospitals is not a state's story.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

MIN_HOSPITALS = 10


def _pct(n: float, d: float) -> str:
    return f"{100.0 * n / d:.0f}%" if d else "n/a"


def _hf(rows: list[list[dict]]) -> dict:
    states = rows[0]
    if len(states) < 2:
        raise ValueError("fewer than two states report enough hospitals")
    worst, best = states[0], states[-1]
    total = sum(r["hospitals"] for r in states)
    overall = sum(r["avg_hf"] * r["hospitals"] for r in states) / total
    gap = worst["avg_hf"] - overall
    return {
        "headline": (f"{worst['state']} has the highest heart-failure readmission rate "
                     f"among states with {MIN_HOSPITALS}+ reporting hospitals"),
        "sentence": (f"Among the {len(states)} states with at least {MIN_HOSPITALS} reporting "
                     f"hospitals, {worst['state']} has the highest average 30-day heart-failure "
                     f"readmission rate ({worst['avg_hf']:.1f}%), {gap:.1f} points above the "
                     f"average across them ({overall:.1f}%). {best['state']} has the lowest "
                     f"({best['avg_hf']:.1f}%)."),
        "metric": f"{worst['avg_hf']:.1f}%",
    }


def _value(rows: list[list[dict]]) -> dict:
    r = rows[0][0]
    hv, rated = r["high_value"], r["rated"]
    if not rated:
        raise ValueError("no hospital has both a star rating and a spending score")
    return {
        "headline": f"{hv} hospitals combine high quality with below-average cost",
        "sentence": (f"{hv} of the {rated} hospitals that have both a star rating and a Medicare "
                     f"spending score ({_pct(hv, rated)}) are rated 4 or 5 stars AND spend less "
                     f"per Medicare patient than the national average (MSPB below 1.0)."),
        "metric": _pct(hv, rated),
    }


def _psych(rows: list[list[dict]]) -> dict:
    states, overall = rows[0], rows[1][0]["median_min"]
    if not states:
        raise ValueError("no state reports enough psychiatric ED times")
    if overall is None:
        raise ValueError("no hospital reports psychiatric ED times")
    top = states[0]
    return {
        "headline": (f"Psychiatric ED patients wait longest in {top['state']}, among states with "
                     f"{MIN_HOSPITALS}+ reporting hospitals"),
        "sentence": (f"Among the {len(states)} states with at least {MIN_HOSPITALS} reporting "
                     f"hospitals, {top['state']}'s typical hospital keeps a psychiatric patient in "
                     f"the emergency department for {top['median_min']:.0f} minutes (the median "
                     f"of its hospitals' median times), against {overall:.0f} minutes for the "
                     f"typical hospital across every reporting hospital in the region, smaller "
                     f"states included."),
        "metric": f"{top['median_min']:.0f} min",
    }


def _lbs(rows: list[list[dict]]) -> dict:
    r = rows[0][0]
    if not r["reporting"]:
        raise ValueError("no hospital reports the left-before-seen rate")
    return {
        "headline": f"{r['over_5']} hospitals lose more than 1 in 20 ED patients before they are seen",
        "sentence": (f"{r['over_5']} of the {r['reporting']} hospitals that report it "
                     f"({_pct(r['over_5'], r['reporting'])}) have more than 5% of emergency "
                     f"patients leave before being seen by a clinician."),
        "metric": _pct(r["over_5"], r["reporting"]),
    }


FINDINGS = [
    {
        "id": "hf_readmission_by_state",
        "title": "Heart-failure readmissions",
        "sql": [f"SELECT state, round(avg(readmit_hf), 1) AS avg_hf, count(readmit_hf) AS hospitals "
                f"FROM gold_hospital_profile WHERE readmit_hf IS NOT NULL GROUP BY state "
                f"HAVING count(readmit_hf) >= {MIN_HOSPITALS} "
                f"ORDER BY avg(readmit_hf) DESC, state"],
        "render": _hf,
    },
    {
        "id": "high_quality_low_cost",
        "title": "Quality and cost together",
        "sql": ["SELECT count(*) FILTER (WHERE star_rating >= 4 AND mspb_score < 1.0) AS high_value, "
                "count(*) FILTER (WHERE star_rating IS NOT NULL AND mspb_score IS NOT NULL) AS rated "
                "FROM gold_hospital_profile"],
        "render": _value,
    },
    {
        "id": "psych_ed_wait",
        "title": "Behavioral health in the ED",
        "sql": [f"SELECT state, round(median(ed_psych_median_min)) AS median_min, count(*) AS hospitals "
                f"FROM gold_hospital_profile WHERE ed_psych_median_min IS NOT NULL GROUP BY state "
                f"HAVING count(*) >= {MIN_HOSPITALS} ORDER BY median(ed_psych_median_min) DESC, state",
                "SELECT round(median(ed_psych_median_min)) AS median_min FROM gold_hospital_profile "
                "WHERE ed_psych_median_min IS NOT NULL"],
        "render": _psych,
    },
    {
        "id": "ed_left_before_seen",
        "title": "Patients leaving the ED unseen",
        "sql": ["SELECT count(*) FILTER (WHERE ed_left_before_seen_pct > 5) AS over_5, "
                "count(ed_left_before_seen_pct) AS reporting FROM gold_hospital_profile"],
        "render": _lbs,
    },
]


def compute(run_sql: Callable[[str], dict], vintage: Optional[str]) -> list[dict]:
    """Every finding, each computed or explained. `run_sql` is the guarded path
    (returns the API's response dict: allowed, decided_by, reason, rows)."""
    out = []
    for f in FINDINGS:
        card: dict[str, Any] = {"id": f["id"], "title": f["title"], "sql": f["sql"],
                                "vintage": vintage}
        try:
            results = []
            for sql in f["sql"]:
                r = run_sql(sql)
                if not r.get("allowed"):
                    raise RuntimeError(f"{r.get('decided_by')}: {r.get('reason')}")
                results.append(r.get("rows") or [])
            card.update(f["render"](results), status="ok")
        except Exception as e:
            card.update(status="unavailable", reason=f"{type(e).__name__}: {e}"[:300])
        out.append(card)
    return out
