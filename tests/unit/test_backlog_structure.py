"""docs/BACKLOG.md stays one entry per item (silent-failure guard).

Twice in one week (items 52-54, then 60-63) a clean git merge of two PRs that
each ticked a neighbouring item left BACKLOG with an item listed twice, once
ticked and once not, and each item's "Done" note shifted under the item above
it. Nothing failed: the file still read as plausible markdown. These checks
fail on exactly that damage.
"""

import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
HEAD = re.compile(r"^- \[([ x])\] \*\*(\d+)\.")
DONE = re.compile(r"^\s+- (?:\*\*)?Done 20\d\d-")


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read().split("\n")


def _items():
    lines = _read("docs/BACKLOG.md")
    out = []
    for i, line in enumerate(lines):
        m = HEAD.match(line)
        if not m:
            continue
        # An item's body ends at the next item, unnumbered entry or heading.
        end = next(
            (j for j in range(i + 1, len(lines)) if lines[j].startswith(("- [", "#"))),
            len(lines),
        )
        out.append((int(m.group(2)), m.group(1) == "x", lines[i + 1 : end]))
    return out


def test_every_item_is_listed_once():
    # (A number may be skipped on purpose -- item 29 once was -- so gaps are fine.)
    numbers = [n for n, _, _ in _items()]
    dupes = sorted({n for n in numbers if numbers.count(n) > 1})
    assert not dupes, f"items listed more than once: {dupes}"


def test_no_conflict_markers_are_left():
    for name in ("docs/BACKLOG.md", "docs/PROGRESS.md"):
        bad = [l for l in _read(name) if re.match(r"^(<{7}|={7}|>{7})( |$)", l)]
        assert not bad, f"{name} has merge conflict markers"


def test_done_notes_sit_under_their_own_ticked_item():
    for n, ticked, body in _items():
        done = [i for i, l in enumerate(body) if DONE.match(l)]
        # An open item may record partial progress as "Done <date> (code part)".
        full = [i for i in done if "part)" not in body[i] and "parts)" not in body[i]]
        assert ticked or not full, f"item {n} is unticked but has a Done note"
        assert len(done) <= 1, f"item {n} has {len(done)} Done notes"
        # Items written in the Where / Done when / Done form keep that order:
        # a Done note above its own "Done when" was shifted in from another item.
        when = [i for i, l in enumerate(body) if l.strip().startswith("- Done when:")]
        if done and when:
            assert done[0] > when[0], f"item {n}: Done note sits above its 'Done when'"


def test_every_item_in_progress_is_ticked():
    ticked = {n for n, t, _ in _items() if t}
    rows = [l for l in _read("docs/PROGRESS.md") if re.match(r"^\| 20\d\d-\d\d-\d\d \|", l)]
    for row in rows:
        # "63. Title" or "8 + 14. Title" is a finished item; "44 (code part)" is not.
        m = re.match(r"^(\d+(?: \+ \d+)*)\. ", row.split("|")[2].strip())
        for n in map(int, re.findall(r"\d+", m.group(1)) if m else []):
            assert n in ticked, f"PROGRESS lists item {n}, which BACKLOG does not tick"
