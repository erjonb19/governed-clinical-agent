"""The guard's parser and engine are pinned the same everywhere they are installed.

setup.py (`pip install -e ".[dev]"`) and requirements-api.txt (CI, Render) must
agree on sqlglot and duckdb. A newer sqlglot changed an internal the PHI check
relies on and made it refuse every FHIR query (fail closed, 2026-11-03); an
unpinned editable install gave the guard no sqlglot at all.
"""

import ast
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _pins(text: str) -> dict:
    return {m.group(1).lower(): m.group(2)
            for m in re.finditer(r"^\s*([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==([^\s#]+)", text, re.M)}


def _setup_guard_pins() -> list:
    tree = ast.parse(open(os.path.join(ROOT, "setup.py"), encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "GUARD_PINS"
                                                for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("setup.py has no GUARD_PINS")


def test_setup_pins_sqlglot_and_duckdb_to_the_api_versions():
    api = _pins(open(os.path.join(ROOT, "requirements-api.txt"), encoding="utf-8").read())
    setup = _pins("\n".join(_setup_guard_pins()))
    for pkg in ("sqlglot", "duckdb"):
        assert pkg in setup, f"setup.py does not pin {pkg}"
        assert setup[pkg] == api[pkg], f"{pkg}: setup.py {setup[pkg]} != requirements-api.txt {api[pkg]}"


def test_the_pins_are_in_install_requires_and_the_dev_extra():
    src = open(os.path.join(ROOT, "setup.py"), encoding="utf-8").read()
    assert "install_requires=requirements + GUARD_PINS" in src
    assert '"dev": GUARD_PINS + [' in src
