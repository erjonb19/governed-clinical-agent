"""
phi_guard.py
============
The PHI rules, one check that every path calls (owner's decision 2026-10-30,
PLAN v2 §0.4). sql_guard.validate_query runs `check` on every query, so /query,
/raw-sql, the approval flow and MCP over stdio and HTTP all go through it: they
all execute through AnalyticsQueryTool, which validates first. After a query
runs, the tool calls `check_output` on the result.

Column labels come from phi_columns.yaml (HIPAA Safe Harbor): identifier,
quasi_identifier (dates are marked, and may be reduced to the year), or safe.
An unlabeled column of a listed table is an identifier. Unlisted tables (the
public CMS hospital data) are not checked.

BEFORE the query runs (`check`), scope by scope through CTEs, subqueries,
derived tables and UNION branches, after `*` is expanded and every column is
qualified:

  1. Row level. A labelled column may reach the result only REDUCED: inside
     COUNT (how many people/records), or -- for a date -- reduced to the year
     (YEAR, EXTRACT/date_part year, date_diff in years). An alias, a cast or a
     string function is not a reduction. Columns used only to filter or join
     (WHERE, ON, window PARTITION BY, an aggregate's FILTER) are not returned.
  2. Aggregates. An identifier may not be a GROUP BY key, and may not sit inside
     MIN, MAX, ANY_VALUE, FIRST, LAST, LIST/ARRAY_AGG, STRING_AGG or their kin.
  3. Small cells. Every aggregate over person-level rows gets
     HAVING count(DISTINCT <person key>) >= 11: a group of fewer than 11 people
     is not returned. An aggregate whose people cannot be counted is refused.

AFTER the query runs (`check_output`), independently: each result column is
traced to its sources with sqlglot's lineage, and any column whose source is
labelled -- whatever it was renamed to -- and was not reduced is refused; so is
any value that is a date with month or day precision or is shaped like a zip
code. Refusals name the column and the rule, never a value.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import sqlglot
import yaml
from sqlglot import exp
from sqlglot.lineage import lineage as _lineage
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phi_columns.yaml")
DIALECT = "duckdb"

IDENTIFIER, QUASI, SAFE = "identifier", "quasi_identifier", "safe"
_RANK = {SAFE: 0, QUASI: 1, IDENTIFIER: 2}

# Aggregates that hand back a member's value rather than a count of members.
_BANNED_AGGS = (exp.Min, exp.Max, exp.AnyValue, exp.First, exp.Last, exp.ArrayAgg,
                exp.GroupConcat, exp.ArgMax, exp.ArgMin, exp.FirstValue, exp.LastValue,
                exp.NthValue)
_BANNED_NAMES = {"min", "max", "any_value", "arbitrary", "first", "last", "list", "array_agg",
                 "string_agg", "group_concat", "listagg", "arg_max", "arg_min", "max_by",
                 "min_by", "first_value", "last_value", "nth_value", "mode", "histogram"}

_DATE_VALUE = re.compile(r"^\s*(\d{4}-\d{1,2}(-\d{1,2})?([ T].*)?|\d{1,2}/\d{1,2}/\d{2,4})\s*$")
_ZIP_VALUE = re.compile(r"^\s*\d{5}(-\d{4})?\s*$")


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def _load(path: str = CONFIG_PATH):
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    tables: dict[str, dict[str, tuple[str, bool]]] = {}
    for t, cols in (doc.get("tables") or {}).items():
        tables[t.lower()] = {}
        for c, label in cols.items():
            if isinstance(label, dict):
                cls, is_date = label.get("class"), bool(label.get("date"))
            else:
                cls, is_date = label, False
            if cls not in _RANK:
                raise ValueError(f"phi_columns.yaml: {t}.{c} has unknown class {cls!r}")
            tables[t.lower()][c.lower()] = (cls, is_date)
    return tables, doc.get("person_key", "patient_id"), int(doc.get("min_cell_size", 11))


TABLES, PERSON_KEY, MIN_CELL = _load()
PROTECTED = set(TABLES)
# for qualify(): expand * and qualify columns of the protected tables
SCHEMA = {t: {c: "VARCHAR" for c in cols} for t, cols in TABLES.items()}


def classify(table: str, column: str) -> tuple[str, bool]:
    """(class, is_date) for a column. Unlisted table: safe. Listed table,
    unlabelled column: identifier (default deny)."""
    t, c = (table or "").lower(), (column or "").lower()
    if t not in TABLES:
        return SAFE, False
    return TABLES[t].get(c, (IDENTIFIER, False))


def touches_protected(tree: exp.Expression) -> bool:
    return any((t.name or "").lower() in PROTECTED for t in tree.find_all(exp.Table))


# ---------------------------------------------------------------------------
# shared expression rules
# ---------------------------------------------------------------------------

def _is_year(node: exp.Expression) -> bool:
    """A reduction of a date to the year (or to an age in whole years)."""
    if isinstance(node, exp.Year):
        return True
    if isinstance(node, exp.Extract):
        return (node.this.name or "").upper() in ("YEAR", "YEARS", "Y")
    if isinstance(node, (exp.DateDiff, exp.DatetimeDiff, exp.TimestampDiff)):
        unit = node.args.get("unit")
        return unit is not None and (unit.name or "").upper() in ("YEAR", "YEARS")
    if isinstance(node, exp.Anonymous) and (node.name or "").lower() in ("date_part", "datepart"):
        first = node.expressions[0] if node.expressions else None
        return isinstance(first, exp.Literal) and first.name.lower() in ("year", "years", "y")
    return False


_DIFFS = (exp.DateDiff, exp.DatetimeDiff, exp.TimestampDiff)
_DATE_LITERAL = re.compile(r"\d{4}-\d")


def _interval_operands(node: exp.Expression) -> Optional[list]:
    """If `node` is a difference between two dates -- date_diff(unit, a, b) in
    any unit, or a - b -- its two operands; else None."""
    if isinstance(node, _DIFFS):
        ops = [node.this, node.expression]
    elif isinstance(node, exp.Anonymous) and (node.name or "").lower() in ("date_diff", "datediff",
                                                                        "date_sub", "datesub"):
        ops = list(node.expressions[1:3])
    elif isinstance(node, exp.Sub):
        ops = [node.this, node.expression]
    else:
        return None
    if len(ops) != 2 or any(o is None for o in ops):
        return None
    # both sides must be columns (cast at most): a literal on either side turns
    # the difference into the date itself, shifted
    for o in ops:
        while isinstance(o, _TRANSPARENT):
            o = o.this
        if not isinstance(o, exp.Column):
            return None
    return ops


# Nodes that change a value's type, not what it says. A reduction counts only
# when nothing else sits between the column and the reducing function: YEAR()
# around arithmetic on a date smuggles the month and day out, and COUNT around
# a CASE on a name is a lookup, not a count of people.
_TRANSPARENT = (exp.Cast, exp.TryCast, exp.Paren, exp.Distinct)


def side_channel(col: exp.Expression, root: exp.Expression) -> Optional[str]:
    """Is `col` used as an aggregate FILTER or a window PARTITION BY / ORDER BY
    inside `root`? Those do not return the value, but on a labelled column they
    look a person up (a filtered count) or form unsuppressed groups (a window)."""
    node = col
    while node is not root and node.parent is not None:
        parent = node.parent
        if isinstance(parent, exp.Filter) and node is parent.args.get("expression"):
            return "an aggregate FILTER"
        if isinstance(parent, exp.Window) and node is not parent.this:
            return "a window PARTITION BY / ORDER BY"
        node = parent
    return None


def _path(col: exp.Expression, root: exp.Expression) -> list:
    """Ancestors of `col` up to and including `root`, nearest first."""
    out, node = [], col
    while node is not root and node.parent is not None:
        out.append(node.parent)
        node = node.parent
    return out


def reduced(col: exp.Expression, root: exp.Expression, is_date: bool) -> bool:
    """Is `col` reduced on its way to `root`: the direct argument of COUNT, or a
    date with YEAR / EXTRACT(year) / date_diff('year') applied to it directly
    (only casts and DISTINCT in between)?"""
    for anc in _path(col, root):
        if isinstance(anc, _TRANSPARENT):
            continue
        if isinstance(anc, exp.Count):
            return True
        if is_date and _is_year(anc):
            return True
        if is_date and _interval_operands(anc) is not None:
            # owner's decision 2 (2026-10-31): days between two dates is not a
            # date. The analysis checks both operands really are date columns.
            return True
        return False
    return False


# The only aggregates allowed over per-patient values (decision 4).
_PER_PATIENT_AGGS = {"count", "sum", "avg", "mean", "stddev", "stddev_samp", "stddev_pop",
                     "variance", "var_samp", "var_pop"}


def _is_key(base: Optional[str]) -> bool:
    """Is `base` ("table.column") a person or record key of a protected table,
    passed through bare? Only such columns may be joined on (decision 3)."""
    if not base or "." not in base:
        return False
    table, column = base.split(".", 1)
    return table in PROTECTED and column.endswith("_id") and \
        classify(table, column)[0] == IDENTIFIER


def _banned_wrapper(col: exp.Expression, root: exp.Expression) -> Optional[str]:
    path = _path(col, root) or []
    for anc in path:
        if isinstance(anc, exp.Count):
            return None
        if isinstance(anc, _BANNED_AGGS):
            return type(anc).__name__.upper()
        if isinstance(anc, exp.Anonymous) and (anc.name or "").lower() in _BANNED_NAMES:
            return anc.name.upper()
    return None


def _duckdb_aggregates() -> set[str]:
    """DuckDB's own aggregate names: sqlglot parses ones it does not model
    (fsum, sumkahan, ...) as plain anonymous functions, which must still count."""
    try:
        import duckdb
        con = duckdb.connect()
        try:
            return {r[0].lower() for r in con.execute(
                "SELECT DISTINCT function_name FROM duckdb_functions() "
                "WHERE function_type = 'aggregate'").fetchall()}
        finally:
            con.close()
    except Exception:
        return set()


_AGG_NAMES = _duckdb_aggregates()


def is_agg(node: exp.Expression) -> bool:
    if isinstance(node, exp.AggFunc):
        return True
    return isinstance(node, exp.Anonymous) and (node.name or "").lower() in _AGG_NAMES


def _is_aggregate(select: exp.Select) -> bool:
    """Does this SELECT collapse rows (GROUP BY or a non-window aggregate)?"""
    if select.args.get("group"):
        return True
    for proj in select.expressions:
        for node in _own_nodes(proj, exp.Expression):
            if is_agg(node) and not isinstance(node.parent, exp.Window) \
                    and not any(isinstance(a, exp.Window) for a in _ancestors(node, proj)):
                return True
    having = select.args.get("having")
    return having is not None


def _equal_components(select: exp.Select):
    """Which column references are tied together by `a = b` in the JOIN ONs and
    the WHERE (top-level AND only: an OR does not tie anything)."""
    parent: dict = {}

    def find(x):
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    def conjuncts(e):
        if isinstance(e, exp.And):
            yield from conjuncts(e.this)
            yield from conjuncts(e.expression)
        elif isinstance(e, exp.Paren):
            yield from conjuncts(e.this)
        elif e is not None:
            yield e

    clauses = [j.args.get("on") for j in (select.args.get("joins") or [])]
    where = select.args.get("where")
    clauses.append(where.this if where is not None else None)
    for clause in clauses:
        for c in conjuncts(clause):
            if isinstance(c, exp.EQ) and isinstance(c.this, exp.Column) and \
                    isinstance(c.expression, exp.Column) and c.this.table and c.expression.table:
                a = f"{c.this.table}.{c.this.name}".lower()
                b = f"{c.expression.table}.{c.expression.name}".lower()
                parent[find(a)] = find(b)
    return lambda a, b: find(a) == find(b)


def _own_nodes(select: exp.Expression, kind):
    """Nodes of `kind` in this SELECT itself, not in its nested queries."""
    stack = [select]
    while stack:
        node = stack.pop()
        if isinstance(node, kind):
            yield node
        for child in node.iter_expressions():
            if isinstance(child, (exp.Subquery, exp.Select)) and child is not select:
                continue
            stack.append(child)


def _ancestors(node, root):
    while node is not root and node.parent is not None:
        node = node.parent
        yield node


# ---------------------------------------------------------------------------
# the pre-execution check
# ---------------------------------------------------------------------------

@dataclass
class Sens:
    cls: str = SAFE
    is_date: bool = False
    base: Optional[str] = None            # "table.column" when passed through raw
    ref: Optional[str] = None             # "alias.column" when a BARE column (or cast)

    def worse(self, other: "Sens") -> "Sens":
        return other if _RANK[other.cls] > _RANK[self.cls] else self


@dataclass
class PhiDecision:
    allowed: bool
    reason: str = "allowed"
    findings: list = field(default_factory=list)
    tree: Optional[exp.Expression] = None
    protected: bool = False
    analysed_sql: Optional[str] = None     # the qualified ORIGINAL query, for check_output
    analysed_names: list = field(default_factory=list)   # its result columns, in order
    aggregate_root: bool = False           # an ungrouped aggregate at the root
    suppressed_scopes: int = 0


class _Analysis:
    def __init__(self, root: exp.Expression):
        self.root = root
        self.memo: dict[int, list[tuple[str, Sens]]] = {}
        self.person: dict[int, Optional[str]] = {}     # person-row scopes: key output name
        self.findings: list[str] = []
        self.rewrites: list[tuple[exp.Select, list]] = []
        self.filters: list[tuple[exp.Filter, list]] = []

    # -- sensitivity of a column reference --
    def resolve(self, scope: Scope, col: exp.Column) -> Sens:
        name, s, src = col.table, scope, None
        if not name:
            # qualify() leaves a column it cannot place unqualified -- e.g. one
            # the config does not know. Place it ourselves: with one source it
            # can only come from there; next to a protected table, fail closed.
            sources = list(scope.sources.values())
            if len(sources) == 1:
                src = sources[0]
            elif any(isinstance(x, exp.Table) and (x.name or "").lower() in PROTECTED
                     for x in sources):
                return Sens(IDENTIFIER, False, None)
        while src is None and s is not None and name:
            src = s.sources.get(name)
            if src is not None:
                break
            s = s.parent
        if src is None:
            # not a source column (a lambda parameter, a struct field...). Fail
            # closed if the name is one a protected table uses for PHI.
            for cols in TABLES.values():
                cls, is_date = cols.get((col.name or "").lower(), (SAFE, False))
                if cls != SAFE:
                    return Sens(IDENTIFIER, False, None)
            return Sens()
        if isinstance(src, exp.Table):
            cls, is_date = classify(src.name, col.name)
            return Sens(cls, is_date, f"{src.name.lower()}.{col.name.lower()}")
        for out_name, sens in self.outputs(src):
            if out_name.lower() == (col.name or "").lower():
                return sens
        return Sens(IDENTIFIER, False, None)            # unknown: fail closed

    def sens_of(self, scope: Scope, proj: exp.Expression) -> Sens:
        worst = Sens()
        for col in proj.find_all(exp.Column):
            if any(isinstance(a, (exp.Subquery, exp.Select)) for a in _ancestors(col, proj)):
                continue                                  # its own scope, below
            s = self.resolve(scope, col)
            if s.cls == SAFE:
                continue
            via = side_channel(col, proj)
            if via:
                self.findings.append(f"{s.cls} {s.base or col.name} used in {via}")
                continue
            if s.cls == IDENTIFIER:
                banned = _banned_wrapper(col, proj)
                if banned:
                    self.findings.append(f"identifier {s.base or col.name} inside {banned}")
            if reduced(col, proj, s.is_date):
                continue
            worst = worst.worse(Sens(s.cls, s.is_date, s.base))
        # A scalar subquery in the SELECT list returns a VALUE into this row --
        # correlated, it is a per-row lookup. Its output's sensitivity is this
        # projection's, unless reduced here like a column.
        # A query nested in the projection returns a VALUE into this row:
        # (SELECT ...), ARRAY(SELECT ...), or any function over one.
        nested = [n for n in proj.find_all(exp.Subquery, exp.Select)
                  if n is not proj and not any(isinstance(a, (exp.Subquery, exp.Select))
                                               for a in _ancestors(n, proj))]
        for sq in nested:
            if isinstance(sq.parent, exp.In):
                continue                                  # x IN (SELECT ...): a filter
            body = sq.this if isinstance(sq, exp.Subquery) else sq
            child = next((c for c in scope.subquery_scopes if c.expression is body), None)
            outs = self.outputs(child) if child is not None else [("?", Sens(IDENTIFIER))]
            for _, s in outs:
                if s.cls == SAFE or reduced(sq, proj, s.is_date):
                    continue
                worst = worst.worse(s)
        inner = proj.this if isinstance(proj, exp.Alias) else proj
        while isinstance(inner, (exp.Cast, exp.TryCast, exp.Paren)):
            inner = inner.this
        if isinstance(inner, exp.Column):
            # a bare column (or a cast of one) passes its source through: it can
            # carry the person key. Anything computed (patient_id || x) cannot.
            r = self.resolve(scope, inner)
            ref = f"{inner.table}.{inner.name}".lower() if inner.table else None
            return Sens(worst.cls, worst.is_date, r.base, ref)
        return Sens(worst.cls, worst.is_date, None, None)

    def outputs(self, scope: Scope) -> list[tuple[str, Sens]]:
        if id(scope) in self.memo:
            return self.memo[id(scope)]
        if scope.union_scopes:
            parts = [self.outputs(s) for s in scope.union_scopes]
            out = []
            for i, (name, sens) in enumerate(parts[0]):
                for other in parts[1:]:
                    if i < len(other):
                        sens = sens.worse(other[i][1])   # keeps the worse branch's source
                out.append((name, sens))
        elif isinstance(scope.expression, exp.Select):
            out = [(p.alias_or_name, self.sens_of(scope, p)) for p in scope.expression.selects]
        else:
            out = []
        self.memo[id(scope)] = out
        return out

    # -- per-scope rules --
    def group_keys(self, scope: Scope) -> None:
        group = scope.expression.args.get("group")
        if not group:
            return
        keys = list(group.expressions)
        if group.args.get("all"):
            keys = [p for p in scope.expression.selects
                    if not any(isinstance(n, exp.AggFunc) for n in p.walk())]
        for key in keys:
            for col in key.find_all(exp.Column):
                s = self.resolve(scope, col)
                if s.cls == IDENTIFIER and not reduced(col, key, s.is_date):
                    if self._per_patient_ok(scope, col, s):
                        continue
                    self.findings.append(f"identifier {s.base or col.name} is a GROUP BY key")

    def _consumers(self, scope: Scope) -> list:
        """The scopes that read this one: as a FROM source, or -- a subquery in a
        predicate -- its parent."""
        users = [s for s in self.scopes if any(v is scope for v in s.sources.values())]
        if users:
            return users
        node = scope.expression.parent
        if isinstance(node, exp.Subquery):
            node = node.parent
        # a semi-join (key IN (SELECT ...)) reads it as a set; a scalar subquery
        # would hand one patient's row straight back, so it has no consumer
        if isinstance(node, exp.In) and scope.parent is not None:
            return [scope.parent]
        return []

    def _per_patient_ok(self, scope: Scope, col: exp.Column, s: "Sens") -> bool:
        """Owner's decision 4 (2026-10-30): grouping BY PATIENT is allowed only
        inside a subquery or CTE whose every consumer aggregates over it. The
        consumer then counts those patient rows (11+ people, by the HAVING the
        rewrite adds), and no patient-level column may reach the result -- the
        root-output rule refuses the patient column if it is passed through."""
        if scope is self.scopes[-1]:
            return False                                   # the result itself
        if not (s.base or "").endswith(f".{PERSON_KEY}"):
            return False                                   # only the person key
        if not self._person_group_refs(scope):
            return False                                   # patient_id AND something
        return self._consumers_aggregate(scope)

    def _consumers_aggregate(self, scope: Scope) -> bool:
        if scope is self.scopes[-1]:
            return False
        users = self._consumers(scope)
        return bool(users) and all(isinstance(u.expression, exp.Select)
                                   and _is_aggregate(u.expression) for u in users)

    def _person_group_refs(self, scope: Scope) -> Optional[list]:
        """If this aggregate is grouped by the person key of its patient
        sources (one row per patient), the bare references it groups by."""
        group = scope.expression.args.get("group")
        if not group or group.args.get("all"):
            return None
        refs = []
        for key in group.expressions:
            inner = key
            while isinstance(inner, (exp.Cast, exp.TryCast, exp.Paren)):
                inner = inner.this
            if not isinstance(inner, exp.Column):
                return None
            r = self.resolve(scope, inner)
            if not (r.base or "").endswith(f".{PERSON_KEY}") or not inner.table:
                return None
            refs.append(f"{inner.table}.{inner.name}".lower())
        if not refs:
            return None
        # Every person source must be tied to a group key by equality: a source
        # joined on anything else (a condition name, gender) puts other
        # people's rows into each "one-patient" group.
        keys = self._person_source(scope)[1] or []
        linked = _equal_components(scope.expression)
        for k in keys:
            k = k.lower()
            if k not in refs and not any(linked(k, r) for r in refs):
                return None
        return refs

    def per_patient_outputs(self, scope: Scope) -> None:
        """Decision 4: no patient-level value reaches the result. A consumer may
        count, sum or average a per-patient scope's values, or group by them
        (11+ people per group), but not pick one patient's out (MAX, ARG_MAX,
        ANY_VALUE...) or collect them all (LIST, STRING_AGG...)."""
        sel = scope.expression
        if not isinstance(sel, exp.Select):
            return
        for alias, src in scope.sources.items():
            if not (isinstance(src, Scope) and isinstance(src.expression, exp.Select)
                    and _is_aggregate(src.expression) and self._person_group_refs(src)):
                continue
            for col in _own_nodes(sel, exp.Column):
                if (col.table or "").lower() != alias.lower():
                    continue
                # an ALLOWLIST: quantiles, bit_or, mode, max... each hands back
                # one patient's value; only these summarise all of them
                for anc in _path(col, sel) or []:
                    if is_agg(anc):
                        name = (anc.name if isinstance(anc, exp.Anonymous)
                                else anc.sql_name()).lower()
                        if name not in _PER_PATIENT_AGGS:
                            self.findings.append(
                                f"a per-patient value {alias}.{col.name} inside {name.upper()} can "
                                f"return one patient's data; use COUNT, SUM or AVG")
                        break

    # --- decision 3: no filtering on identifying columns ---
    def predicates(self, scope: Scope) -> None:
        """Owner's decision 3 (2026-10-30): an identifier or quasi-identifier may
        not be used to FILTER -- in WHERE, HAVING, QUALIFY or JOIN ON, at any
        level (subqueries are scopes of their own and are checked the same way).
        Allowed: a year (or age in whole years) reduction of a date; COUNT of a
        column; a key equality between identifier columns (a join, not a
        filter); a key semi-join (patient_id IN (SELECT patient_id ...)); and
        IS [NOT] NULL, which tests presence, not value."""
        sel = scope.expression
        joins = sel.args.get("joins") or []
        if any((j.args.get("method") or "").upper() == "NATURAL" for j in joins) and \
                self._person_source(scope)[0]:
            # NATURAL joins on every shared name -- given_name included -- with no
            # ON clause to check
            self.findings.append("a NATURAL JOIN over patient data joins on columns it does "
                                 "not name; use JOIN ... ON the key")
        clauses = [sel.args.get("where"), sel.args.get("having"), sel.args.get("qualify")]
        clauses += [j.args.get("on") for j in joins]
        for clause in clauses:
            if clause is None:
                continue
            for col in _own_nodes(clause, exp.Column):
                s = self.resolve(scope, col)
                if s.cls == SAFE or reduced(col, clause, s.is_date):
                    continue
                if self._key_join(scope, col, s):
                    continue
                if s.is_date and self._date_comparison(scope, col, clause):
                    continue
                if isinstance(col.parent, exp.Is) and isinstance(col.parent.expression, exp.Null) \
                        and s.base:
                    continue                    # presence of a real column, not its value
                self.findings.append(f"{s.cls} {s.base or col.name} used to filter (WHERE / "
                                     f"HAVING / JOIN ON); filter on safe columns, or on the year")
        # ORDER BY an identifying column sorts people by who they are: with a
        # LIMIT it singles one out, without one it lines rows up per person
        order = sel.args.get("order")
        if order is not None:
            aliases = {p.alias.lower(): p for p in sel.expressions if p.alias}
            for col in _own_nodes(order, exp.Column):
                if not col.table and (col.name or "").lower() in aliases:
                    # ORDER BY an output alias: as sensitive as that output
                    s = self.sens_of(scope, aliases[col.name.lower()])
                    if s.cls != SAFE:
                        self.findings.append(f"{s.cls} {s.base or col.name} used to ORDER BY")
                    continue
                s = self.resolve(scope, col)
                if s.cls != SAFE and not reduced(col, order, s.is_date):
                    self.findings.append(f"{s.cls} {s.base or col.name} used to ORDER BY")

    def intervals(self, scope: Scope) -> None:
        """A date difference counts as an interval only between two real date
        columns: a CTE holding a constant date would otherwise read a birth date
        back to the day as an 'interval' from it."""
        for node in _own_nodes(scope.expression, exp.Expression):
            ops = _interval_operands(node)
            if ops is None:
                continue
            cols = [next(o.find_all(exp.Column)) for o in ops]
            sens = [self.resolve(scope, c) for c in cols]
            if not any(x.cls != SAFE for x in sens):
                continue                                    # no patient dates involved
            if not all(x.is_date and x.base for x in sens):
                self.findings.append("a date difference must be between two date columns, "
                                     "not a date and a constant")

    def _date_comparison(self, scope: Scope, col: exp.Column, clause: exp.Expression) -> bool:
        """Owner's decision 2: a filter may compare two date columns (b.start >
        a.end, b.start BETWEEN a.end AND a.end + INTERVAL 30 DAY). Not a date
        against a literal more specific than the year."""
        node = col
        while node.parent is not None and node is not clause:
            parent = node.parent
            if isinstance(parent, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)):
                break
            if isinstance(parent, _TRANSPARENT):
                node = parent
                continue
            if isinstance(parent, (exp.Add, exp.Sub)) and \
                    isinstance(parent.expression, exp.Interval):
                node = parent                                # a.end + INTERVAL 30 DAY
                continue
            return False
        else:
            return False
        cmp = node.parent
        cols = list(_own_nodes(cmp, exp.Column))
        for c in cols:
            r = self.resolve(scope, c)
            if not (r.is_date and r.base):
                return False
        if len({(c.table, c.name) for c in cols}) < 2:
            return False
        for lit in cmp.find_all(exp.Literal):
            if lit.is_string and _DATE_LITERAL.search(lit.this or "") and \
                    not isinstance(lit.parent, exp.Interval):
                return False
        return True

    def aggregate_only(self, scopes: list) -> None:
        """Owner's decision 1 (2026-10-31): patient data is aggregate-only. The
        result must be an aggregate (each row covering 11+ people, by the
        small-cell rule); row-level results are refused even of safe columns."""
        root = scopes[-1]
        branches = root.union_scopes or [root]
        stack = list(branches)
        while stack:
            sc = stack.pop()
            if sc.union_scopes:
                stack.extend(sc.union_scopes)
                continue
            if isinstance(sc.expression, exp.Select) and self._person_source(sc)[0] and \
                    not _is_aggregate(sc.expression):
                self.findings.append("returns patient records row by row; patient data is "
                                     "returned only as aggregates (counts, averages, rates) "
                                     f"covering {MIN_CELL} or more people")
                return

    def _key_join(self, scope: Scope, col: exp.Column, s: "Sens") -> bool:
        """A join, not a filter: a key of a protected table equated to another
        such key, or tested against a set of them (key IN (SELECT key ...)).
        A constant dressed as a key -- VALUES, a literal concatenated onto one,
        IN (SELECT '<id>') -- is not a key, so it is a filter."""
        if not _is_key(s.base):
            return False
        node, parent = col, col.parent
        while isinstance(parent, (exp.Cast, exp.TryCast, exp.Paren)):
            node, parent = parent, parent.parent
        if isinstance(parent, (exp.EQ, exp.NEQ)):
            other = parent.expression if parent.this is node else parent.this
            while isinstance(other, (exp.Cast, exp.TryCast, exp.Paren)):
                other = other.this
            return isinstance(other, exp.Column) and _is_key(self.resolve(scope, other).base)
        if isinstance(parent, exp.In) and parent.this is node and parent.args.get("query") is not None:
            q = parent.args["query"]
            q = q.this if isinstance(q, exp.Subquery) else q
            sub_scope = next((x for x in self.scopes if x.expression is q), None)
            return sub_scope is not None and self._selects_keys(sub_scope)
        return False

    def _selects_keys(self, scope: Scope) -> bool:
        if scope.union_scopes:
            return all(self._selects_keys(u) for u in scope.union_scopes)
        outs = self.outputs(scope)
        return len(outs) == 1 and _is_key(outs[0][1].base)

    def person_rows(self, scope: Scope) -> tuple[bool, Optional[list]]:
        """Does this scope's output hold person-level rows, and if so which
        output columns carry the person key of EVERY person source it reads?
        (None when any is missing: a join on gender between two patient tables
        would otherwise hand the outer query one key over a fanned-out set.)"""
        if id(scope) in self.person:
            r = self.person[id(scope)]
            return r is not False, (r if isinstance(r, list) else None)
        result: Any = False
        if scope.union_scopes:
            result = False if not any(self.person_rows(s)[0] for s in scope.union_scopes) else None
        elif isinstance(scope.expression, exp.Select) and _is_aggregate(scope.expression) \
                and self._person_group_refs(scope):
            # grouped by patient (decision 4): one row per patient, and the
            # outer aggregate must count them -- so it carries the key
            refs = self._person_group_refs(scope)
            outs = self.outputs(scope)
            names = [next((n for n, sens in outs if sens.ref == r), None) for r in refs]
            result = names if all(names) else None
        elif isinstance(scope.expression, exp.Select) and not _is_aggregate(scope.expression):
            found, keys = self._person_source(scope)
            if found:
                result = None
                if keys:
                    outs = self.outputs(scope)
                    names = []
                    for k in keys:
                        name = next((n for n, sens in outs if sens.ref == k.lower()), None)
                        if name is None:
                            names = None
                            break
                        names.append(name)
                    result = names
        self.person[id(scope)] = result
        return result is not False, (result if isinstance(result, list) else None)

    def _person_source(self, scope: Scope) -> tuple[bool, Optional[str]]:
        """(reads person rows?, the person keys of EVERY person source joined
        with AND, or None if any of them has none). Every source counts: a
        cross join to an unrelated patient table must not pad a group's count."""
        found, keys, missing = False, [], False
        for alias, src in scope.sources.items():
            if isinstance(src, exp.Table):
                t = (src.name or "").lower()
                if t in PROTECTED:
                    found = True
                    if PERSON_KEY in TABLES[t]:
                        keys.append(f"{alias}.{PERSON_KEY}")
                    else:
                        missing = True
            elif isinstance(src, Scope):
                rows, ks = self.person_rows(src)
                if rows:
                    found = True
                    if ks:
                        keys.extend(f"{alias}.{k}" for k in ks)
                    else:
                        missing = True
        return found, (None if missing or not keys else keys)

    def small_cells(self, scope: Scope) -> None:
        sel = scope.expression
        if not isinstance(sel, exp.Select) or not _is_aggregate(sel):
            return
        found, key = self._person_source(scope)
        if not found:
            return
        # A conditional aggregate carves a subgroup the group's HAVING does not
        # see (count(*) FILTER (WHERE age_years > 105) inside a group of 1,180).
        # FILTER is suppressed per aggregate below; a condition hidden inside an
        # aggregate's argument cannot be, so it is refused.
        for agg in _own_nodes(sel, exp.Expression):
            if not is_agg(agg) or isinstance(agg.parent, exp.Window):
                continue
            conditional = isinstance(agg, exp.CountIf) or \
                (isinstance(agg, exp.Anonymous) and (agg.name or "").lower().endswith("_if"))
            if conditional or any(isinstance(n, (exp.Case, exp.If, exp.Predicate, exp.Is,
                                                 exp.Like, exp.ILike, exp.Not, exp.Connector))
                                  for n in agg.walk() if n is not agg):
                self.findings.append(
                    f"a condition inside an aggregate ({agg.sql(dialect=DIALECT)[:50]}) over "
                    f"patient rows forms a subgroup that cannot be held to {MIN_CELL} people; "
                    "use FILTER (WHERE ...) or GROUP BY instead")
        if key is None:
            self.findings.append(
                "an aggregate over patient records without a patient key, so groups of fewer "
                f"than {MIN_CELL} people cannot be suppressed; keep {PERSON_KEY} in the inner "
                "query, or aggregate the tables directly")
            return
        if self._person_group_refs(scope) and self._consumers_aggregate(scope):
            # one group per patient (decision 4): each is one person by design,
            # so the 11-person rule belongs to the outer aggregate counting them
            return
        self.rewrites.append((sel, key))
        self.filters.extend((f, key) for f in _own_nodes(sel, exp.Filter)
                            if not isinstance(f.parent, exp.Window))

    def windows(self, scope: Scope) -> None:
        """Window functions over patient rows: an aggregate OVER a partition is a
        group the small-cell rule cannot suppress, so it is refused; a labelled
        column in any window (projections, QUALIFY, ORDER BY) is refused too."""
        sel = scope.expression
        reads_patients = self._person_source(scope)[0]
        grouped = isinstance(sel, exp.Select) and _is_aggregate(sel)
        for win in _own_nodes(sel, exp.Window):
            over_groups = grouped and any(is_agg(n) for n in win.this.walk() if n is not win.this)
            if reads_patients and is_agg(win.this) and not over_groups:
                self.findings.append(
                    f"a window aggregate ({win.this.sql(dialect=DIALECT)[:40]} OVER ...) over "
                    f"patient rows, whose groups cannot be held to {MIN_CELL} people; use GROUP BY")
            for col in win.find_all(exp.Column):
                if col.parent is win.this or any(a is win.this for a in _ancestors(col, win)):
                    continue
                s = self.resolve(scope, col)
                if s.cls != SAFE:
                    self.findings.append(f"{s.cls} {s.base or col.name} used in a window "
                                         f"PARTITION BY / ORDER BY")

    def run(self) -> None:
        # COLUMNS('regex') / COLUMNS(lambda) select columns by pattern, so no
        # column reference exists to classify; a * qualify() could not expand
        # is the same. On patient data both are refused.
        for node in self.root.walk():
            if isinstance(node, exp.Columns) or (
                    isinstance(node, exp.Anonymous) and (node.name or "").lower() == "columns"):
                self.findings.append("COLUMNS(...) selects columns by pattern; name them instead")
                break
        if self.root.find(exp.PositionalColumn):
            self.findings.append("a positional column reference (#N); name the column")
        for star in self.root.find_all(exp.Star):
            if not isinstance(star.parent, (exp.Count, exp.AggFunc)):
                self.findings.append("an unexpanded * over patient data; name the columns")
                break
        # a query inside a LIMIT / OFFSET, or as a table function's argument,
        # sizes the result by its value: refused outright on patient data
        for node in self.root.find_all(exp.Limit, exp.Offset, exp.Fetch):
            if node.find(exp.Select):
                self.findings.append("a query inside LIMIT / OFFSET; use a number")
                break
        for t in self.root.find_all(exp.Table):
            if isinstance(t.this, exp.Func) and t.this.find(exp.Select):
                self.findings.append("a query as a table function's argument")
                break
        scopes = list(traverse_scope(self.root))
        self.scopes = scopes
        self._row_sources = []
        for sc in scopes:
            for src in sc.sources.values():
                if isinstance(src, Scope):
                    self._row_sources.append(src)
            self._row_sources.extend(sc.union_scopes or [])
        for scope in scopes:
            self.outputs(scope)
            if isinstance(scope.expression, exp.Select):
                self.group_keys(scope)
                self.small_cells(scope)
                self.windows(scope)
                self.predicates(scope)
                self.per_patient_outputs(scope)
                self.intervals(scope)
        if scopes:
            self.aggregate_only(scopes)
        # A query nested anywhere but FROM -- WHERE, HAVING, ON, a LIMIT, a
        # function's or table function's argument -- hands its value straight
        # into the outer query, where it can be compared, counted or turned into
        # a row count. Its outputs are held to the result's rule: safe only.
        # (FROM sources are followed by lineage; UNION branches belong to their
        # union; a key IN-set is a semi-join.)
        for scope in scopes[:-1]:
            if scope in self._row_sources:
                continue
            node = scope.expression.parent
            if isinstance(node, exp.Subquery):
                node = node.parent
            if isinstance(node, exp.In) and self._selects_keys(scope):
                continue
            for name, sens in self.outputs(scope):
                if sens.cls != SAFE:
                    self.findings.append(f"a nested query returns {sens.cls} {sens.base or name} "
                                         f"into the outer query")
        root_scope = scopes[-1] if scopes else None
        if root_scope is not None:
            for name, sens in self.outputs(root_scope):
                if sens.cls != SAFE:
                    what = "a date with month or day precision" if sens.is_date else sens.cls
                    self.findings.append(f"returns {what} {sens.base or name} as column {name!r}")


def _rule_hint(findings: list[str]) -> str:
    return ("Patient data may only be returned aggregated: counts of people, or dates "
            f"reduced to the year; groups of fewer than {MIN_CELL} people are not shown.")


# Table functions whose output is plain generated rows. Any other function in
# FROM -- query('<sql>'), query_table('<name>'), an unknown one -- hides what it
# reads from this check (query() even hides it from the allowlist), so it is
# refused: default deny.
_SAFE_TABLE_FUNCTIONS = {"range", "generate_series", "unnest"}


def check(tree: exp.Expression) -> PhiDecision:
    """The pre-execution check. `tree` is one parsed SELECT (sql_guard has
    already enforced SELECT-only, the allowlist and the function list)."""
    for t in tree.find_all(exp.Table):
        if isinstance(t.this, (exp.Identifier, type(None))):
            continue
        fn = (t.this.name if isinstance(t.this, exp.Anonymous) else
              (t.this.sql_name() if isinstance(t.this, exp.Func) else "")).lower()
        if fn not in _SAFE_TABLE_FUNCTIONS:
            return PhiDecision(False, f"PHI rule: the table function {fn or '?'}() reads data the "
                                      f"guard cannot see, denied (fail closed)",
                               findings=[fn], protected=True)
    if not touches_protected(tree):
        return PhiDecision(True, tree=tree)
    try:
        q = qualify(tree.copy(), schema=SCHEMA, dialect=DIALECT,
                    validate_qualify_columns=False, quote_identifiers=False)
    except Exception as e:
        return PhiDecision(False, f"PHI check could not analyse the query, denied (fail closed): "
                                  f"{type(e).__name__}", protected=True)
    a = _Analysis(q)
    try:
        a.run()
    except Exception as e:
        return PhiDecision(False, f"PHI check could not analyse the query, denied (fail closed): "
                                  f"{type(e).__name__}", protected=True)
    if a.findings:
        uniq = list(dict.fromkeys(a.findings))
        return PhiDecision(False, f"PHI rule: {'; '.join(uniq)}. {_rule_hint(uniq)}",
                           findings=uniq, protected=True)
    # The copy check_output traces: every result column given a name, so the
    # database's own column names (count_star()) map to it by position.
    named = q.copy()
    first = named
    while isinstance(first, exp.SetOperation):
        first = first.this
    names = []
    if isinstance(first, exp.Select):
        projs = []
        for i, p in enumerate(first.expressions):
            if not p.alias:
                p = exp.alias_(p, f"__col{i}")
            projs.append(p)
            names.append(p.alias)
        first.set("expressions", projs)
    analysed = named.sql(dialect=DIALECT)

    # Suppression is written into the user's ORIGINAL query, not the qualified
    # copy, so the result keeps the column names the database gives it.
    # qualify() adds and removes no SELECTs, so they correspond in walk order;
    # qualify() also names every table, which is what makes "alias.patient_id"
    # resolvable in the original. If the shapes ever disagree, execute the
    # qualified copy instead -- correct, only with generated column names.
    out_tree = tree.copy()
    q_selects = list(q.find_all(exp.Select))
    o_selects = list(out_tree.find_all(exp.Select))
    unaliased = any(isinstance(sq.parent, (exp.From, exp.Join)) and not sq.alias
                    for sq in tree.find_all(exp.Subquery))
    if len(q_selects) != len(o_selects) or unaliased:
        # (an unaliased derived table: only the qualified copy has the name the
        # rewrite refers to)
        out_tree, o_selects = q, q_selects
    def _enough(keys):
        return sqlglot.parse_one(" AND ".join(f"count(DISTINCT {k}) >= {MIN_CELL}" for k in keys),
                                 read=DIALECT)

    # FILTERed aggregates first (their nodes are found by position in the
    # copy), each nulled unless its own subgroup holds 11+ people
    q_filters = list(q.find_all(exp.Filter))
    o_filters = list(out_tree.find_all(exp.Filter))
    for flt, keys in a.filters:
        target = o_filters[next(i for i, x in enumerate(q_filters) if x is flt)]
        where_sql = target.args["expression"].sql(dialect=DIALECT)          # "WHERE ..."
        guard = " AND ".join(f"count(DISTINCT {k}) FILTER ({where_sql}) >= {MIN_CELL}"
                             for k in keys)
        gated = sqlglot.parse_one(f"CASE WHEN {guard} THEN {target.sql(dialect=DIALECT)} END",
                                  read=DIALECT)
        if isinstance(target.parent, exp.Select):     # unaliased: keep the name it had
            gated = exp.alias_(gated, target.sql(dialect=DIALECT), quoted=True)
        target.replace(gated)
    for sel, keys in a.rewrites:
        target = o_selects[next(i for i, x in enumerate(q_selects) if x is sel)]
        cond = _enough(keys)
        having = target.args.get("having")
        target.set("having", exp.Having(this=exp.and_(having.this, cond) if having else cond))
    root_sel = q if isinstance(q, exp.Select) else None
    return PhiDecision(True, tree=out_tree, protected=True, analysed_sql=analysed, analysed_names=names,
                       aggregate_root=bool(root_sel is not None and _is_aggregate(root_sel)
                                           and not root_sel.args.get("group")),
                       suppressed_scopes=len(a.rewrites))


# ---------------------------------------------------------------------------
# the post-execution check
# ---------------------------------------------------------------------------

def _public_tables() -> set[str]:
    from sql_guard import ALLOWED_TABLES
    return {t.lower() for t in ALLOWED_TABLES} - PROTECTED


_PUBLIC_TABLES = _public_tables()


class OutputRefused(Exception):
    """A result that must not be returned. The message never holds a value."""


def _trace(node) -> list[tuple[str, bool, str, bool]]:
    """(source "table.column", reduced on the way?, class, is_date) for every
    lineage path under `node`. A path is reduced if ANY step reduces it."""
    out: list[tuple[str, bool, str, bool]] = []
    if not node.downstream:
        return out
    for child in node.downstream:
        leaf = not child.downstream
        src_name = child.name.replace('"', "").lower()
        if leaf:
            table, _, column = src_name.rpartition(".")
            # lineage names the leaf by its ALIAS (o.value_num); the node's
            # source is the real table. Without one, it cannot be placed.
            if isinstance(child.source, exp.Table) and child.source.name:
                table = child.source.name.lower()
                src_name = f"{table}.{column}"
            if isinstance(child.source, exp.Placeholder) or child.expression.find(exp.Placeholder):
                cls, is_date = IDENTIFIER, False        # lineage lost the trail: fail closed
            elif not isinstance(child.source, exp.Table) and \
                    not any(True for _ in child.expression.find_all(exp.Column)):
                cls, is_date = SAFE, False              # a constant or COUNT(*): no source
            elif table not in PROTECTED and table not in _PUBLIC_TABLES:
                cls, is_date = IDENTIFIER, False        # cannot place it: fail closed
            else:
                cls, is_date = classify(table, column)
            reduced_here = _reduces(node.expression, child.name, is_date)
            out.append((src_name, reduced_here or cls == SAFE, cls, is_date))
        else:
            for src, red, cls, is_date in _trace(child):
                if not red:
                    red = _reduces(node.expression, child.name, is_date)
                out.append((src, red, cls, is_date))
    return out


def _reduces(expression: exp.Expression, ref: str, is_date: bool) -> bool:
    """In this step's projection, is every reference to `ref` reduced?"""
    table, _, column = ref.replace('"', "").lower().rpartition(".")
    refs = [c for c in expression.find_all(exp.Column)
            if (c.name or "").lower() == column and (not table or (c.table or "").lower() == table)]
    return bool(refs) and all(reduced(c, expression, is_date) for c in refs)


def check_output(decision: Any, columns: list[str], rows: list[dict]) -> None:
    """Raise OutputRefused if the result carries PHI. `decision` is the
    validated query's sql_guard.Decision (with .phi_sql)."""
    sql = getattr(decision, "phi_sql", None)
    if not sql:
        return
    names = list(getattr(decision, "phi_names", None) or [])
    if len(names) != len(columns):
        raise OutputRefused(f"output check: the result has {len(columns)} columns but the "
                            f"query analysed has {len(names)}, so it is not returned (fail closed)")
    for col, name in zip(columns, names):
        try:
            node = _lineage(name, sql, schema=SCHEMA, dialect=DIALECT)
            paths = _trace(node)
        except Exception:
            raise OutputRefused(f"output check: column {col!r} could not be traced to its "
                                f"sources, so it is not returned (fail closed)")
        for src, red, cls, is_date in paths:
            if cls != SAFE and not red:
                raise OutputRefused(f"output check: column {col!r} comes from {cls} {src}")
    for col in columns:
        for r in rows:
            for v in _leaves(r.get(col)):     # lists, structs and maps hold values too
                if isinstance(v, (_dt.date, _dt.datetime)) and not isinstance(v, _dt.time):
                    raise OutputRefused(f"output check: column {col!r} holds dates with month "
                                        f"or day precision")
                if isinstance(v, str):
                    if _DATE_VALUE.match(v):
                        raise OutputRefused(f"output check: column {col!r} holds dates with "
                                            f"month or day precision")
                    if _ZIP_VALUE.match(v):
                        raise OutputRefused(f"output check: column {col!r} holds values shaped "
                                            f"like a zip code")


def _leaves(v):
    """Every scalar inside a value, through lists, tuples, structs and maps."""
    if isinstance(v, dict):
        for x in list(v.keys()) + list(v.values()):
            yield from _leaves(x)
    elif isinstance(v, (list, tuple, set)):
        for x in v:
            yield from _leaves(x)
    else:
        yield v
