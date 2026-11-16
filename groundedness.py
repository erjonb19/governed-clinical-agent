"""
groundedness.py
===============
Verifies that every number in a brief traces back to a row a query actually
returned. The agent cannot commit a figure it did not retrieve from Gold.

Design:
  QueryLedger  -- the analytics tool records each successful query's real rows
                  here at execution time (before redaction), keyed by query_id.
  Claim        -- a structured assertion: a value, the column it came from, the
                  row dimensions that locate it, and optionally the query_id.
  check_claims -- every claim must match a real row in the ledger, or the brief
                  is not grounded and must not commit.

Why structured claims: verification is exact. We never guess whether a number in
prose is a data point or a hallucination. Each claim names its own evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class QueryLedger:
    """In-process record of what each query returned, for this session."""

    def __init__(self) -> None:
        self._queries: Dict[int, Dict[str, Any]] = {}
        self._next_id = 1

    def record(self, columns: List[str], rows: List[Dict[str, Any]]) -> int:
        qid = self._next_id
        self._next_id += 1
        self._queries[qid] = {"columns": list(columns), "rows": list(rows)}
        return qid

    def get(self, qid: int) -> Optional[Dict[str, Any]]:
        return self._queries.get(qid)

    def all_queries(self) -> Dict[int, Dict[str, Any]]:
        return dict(self._queries)


@dataclass
class Claim:
    value: float                       # the asserted number
    metric: str                        # the column it should come from
    dims: Dict[str, Any] = field(default_factory=dict)   # row locators, e.g. {"region": "Bronx"}
    source_query_id: Optional[int] = None
    label: str = ""                    # optional human description for messages

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Claim":
        return Claim(
            value=float(d["value"]),
            metric=str(d["metric"]),
            dims=dict(d.get("dims") or {}),
            source_query_id=d.get("source_query_id"),
            label=str(d.get("label") or ""),
        )


@dataclass
class ClaimResult:
    grounded: bool
    claim: Claim
    reason: str
    matched_query_id: Optional[int] = None


@dataclass
class GroundednessResult:
    ok: bool
    findings: List[ClaimResult]

    @property
    def ungrounded(self) -> List[ClaimResult]:
        return [f for f in self.findings if not f.grounded]


def _num_match(a: float, b: float, abs_tol: float = 0.05, rel_tol: float = 0.001) -> bool:
    """Match two numbers allowing for rounding in the brief."""
    diff = abs(a - b)
    return diff <= abs_tol or diff <= rel_tol * max(abs(a), abs(b))


def _dims_match(claim_dims: Dict[str, Any], row: Dict[str, Any]) -> bool:
    for k, v in claim_dims.items():
        if k not in row:
            return False
        rv = row[k]
        # numbers compared numerically, everything else as lowercased strings
        if isinstance(v, (int, float)) and isinstance(rv, (int, float)):
            if not _num_match(float(v), float(rv)):
                return False
        else:
            if str(rv).strip().lower() != str(v).strip().lower():
                return False
    return True


def _row_supports(claim: Claim, row: Dict[str, Any]) -> bool:
    if claim.metric not in row:
        return False
    cell = row[claim.metric]
    if not isinstance(cell, (int, float)):
        return False
    if not _num_match(claim.value, float(cell)):
        return False
    return _dims_match(claim.dims, row)


_NUMBER = __import__("re").compile(r"(?<![\w.])-?\d+(?:,\d{3})*(?:\.\d+)?")


def numbers_in(text: Optional[str]) -> List[float]:
    """Every number written in free text (thousands separators allowed)."""
    return [float(m.group(0).replace(",", "")) for m in _NUMBER.finditer(text or "")]


def ungrounded_numbers(edited: str, sources: List[Optional[str]]) -> List[float]:
    """Audit item 32: numbers in a reviewer's edited text that appear in none of
    the sources (the evidence rows, the original proposal, the question). An
    edit may reword a brief; it may not introduce a figure nobody retrieved."""
    known = [n for s in sources for n in numbers_in(s)]
    return [n for n in numbers_in(edited) if not any(_num_match(n, k) for k in known)]


def check_claims(claims: List[Claim], ledger: QueryLedger) -> GroundednessResult:
    findings: List[ClaimResult] = []

    for claim in claims:
        # which queries to search: the named one, or all of them
        if claim.source_query_id is not None:
            q = ledger.get(claim.source_query_id)
            candidates = {claim.source_query_id: q} if q else {}
            if not q:
                findings.append(ClaimResult(
                    False, claim,
                    f"source_query_id {claim.source_query_id} not found in ledger"))
                continue
        else:
            candidates = ledger.all_queries()

        matched_qid = None
        for qid, q in candidates.items():
            if any(_row_supports(claim, row) for row in q["rows"]):
                matched_qid = qid
                break

        desc = claim.label or f"{claim.metric}={claim.value} {claim.dims or ''}".strip()
        if matched_qid is not None:
            findings.append(ClaimResult(True, claim, f"matched in query {matched_qid}", matched_qid))
        else:
            findings.append(ClaimResult(
                False, claim,
                f"no returned row supports {desc} (possible hallucination)"))

    ok = all(f.grounded for f in findings)
    return GroundednessResult(ok=ok, findings=findings)
