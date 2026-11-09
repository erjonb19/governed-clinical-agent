# Plan: one package with layers (backlog item 18)

**Status: plan only, waiting for the owner's OK (2026-11-09).** Nothing has moved.

## Where things are today

- 49 modules at the repo root, beside `src/` (the Milestone-1 runtime:
  `src/runtime`, `src/security`, `src/policies`, `src/tools`, `src/utils`).
  `setup.py` packages `src` (`find_packages()`), but nothing at the root, so the
  root modules are only importable from a checkout with the repo on `sys.path`;
  most of them do `sys.path.insert(0, ...)`.
- 41 distinct settings read with `os.environ.get(...)` across 19 root modules,
  several read in more than one place (`HOSPITAL_GOLD_DB` 7 times,
  `PLANNER_SCHEMA` 5) and some at **import time** (`nl_to_sql_planner` binds
  `SCHEMA_DOC`/`SYSTEM_PROMPT` from `PLANNER_SCHEMA` when imported, which is why
  `eval_harness.py` must assign it before importing).
- `app.py` builds the app at import (`app = FastAPI(...)`), with its state in a
  module-level `_STATE` dict; tests reload the module to get a fresh app.

## Target layout

```
governed_agent/                 # the one package (name open: see "Decisions")
  api/          app factory (create_app), routes, the /v1 prefix, auth, rate limits
                  <- app.py, findings.py, mcp_server.py
  agent/        planner, critic, graphs, approvals, replay
                  <- nl_to_sql_planner.py, critic.py, agent_graph.py,
                     approval.py, approval_graph.py, replay.py, versioning.py
  guard/        sql_guard.py, phi_guard.py, phi_columns.yaml, schema_check.py
                  (moved LAST and ALONE, see "Order")
  data/         backends, the analytics tool, Gold builders and quality gates
                  <- data_backends.py, analytics_query_tool.py, data_quality.py,
                     gold_history.py, build_*.py, fetch_*.py
  evals/        harness, banks, trajectory scoring, scheduled runs, alerts
                  <- eval_harness.py, eval_bank*.py, trajectory_eval.py,
                     scheduled_runs.py, nightly_alerts.py, validate_*.py
  observability/ tracing, export, cost, metrics
                  <- tracing.py, trace_export.py, cost_tracker.py, aiops_metrics.py
  settings.py   ONE typed settings object (pydantic-settings), read once
src/            the Milestone-1 runtime, unchanged (it already is a package)
scripts/, demo_*.py, aiops_panel.py, live_check.py   stay where they are (entry
                  points, not library code), importing from the package
```

### Settings

A `Settings` class (pydantic-settings, already a transitive dependency of the
stack via `mcp`) with every variable, its type, default and one-line meaning, in
groups (planner, data, auth, tracing, approvals, evals). Modules take the values
from `get_settings()` at **call** time, not import time, so tests set them per
test instead of reloading modules. The README's configuration table is generated
from it, so docs and code cannot drift.

### App factory

`create_app(settings=None) -> FastAPI` replaces the module-level app: the
lifespan builds the runtime, planner, graphs and tracer from the settings, and
`app.py` keeps `app = create_app()` so `uvicorn app:app`, Render's start
command and the Dockerfile do not change.

## Order (each step one small PR, no behaviour change)

1. `settings.py` + `create_app`, with the current modules reading from them;
   no file moves yet. The full suite, the replayed push gate and the E2Es green.
2. `observability/` (no dependants inside the guard).
3. `data/`, then `agent/`, then `evals/`, then `api/`. Each move leaves a
   **shim** at the old path (`from governed_agent.agent.critic import *` plus
   the module's private names the tests use), so every existing import, script,
   workflow and doc keeps working.
4. `guard/` **last and alone**: `sql_guard.py`, `phi_guard.py`,
   `phi_columns.yaml`, `schema_check.py`. A PR the owner merges (it changes
   where the guard lives). Its checks: the guard's editable-install job, the
   adversarial harness (`python sql_guard.py`), the full security suite and the
   manual FHIR eval.
5. Remove the shims, in one PR, once nothing imports the old paths
   (`git grep` and a test that fails if a root shim is imported).

## What must survive

- **CLAUDE.md rule 6** (schema and planner agree): `schema_check.py` keeps
  running at graph startup; the move does not change `SCHEMA_DOC`.
- **The guard's pinned install**: `setup.py` `GUARD_PINS` and the
  `guard-editable-install` job; the package's `install_requires` keep the same
  pins; `tests/unit/test_dependency_pins.py` keeps passing.
- Render's start command (`uvicorn app:app`), the Dockerfile and every workflow,
  unchanged until step 5.
- The replay recordings (item 13): a move must not change any prompt text, so
  `tests/unit/test_push_gate_recordings.py` passes without re-recording.

## Checks for every step

`pytest` (with the item 15 gates), `ui-e2e`, `mcp-e2e`, the replayed push gate,
`python sql_guard.py`, and `git grep` for the old import path in docs and
workflows. No eval sweep is needed: nothing a model sees changes.

## Decisions for the owner

1. **Go or no-go** on this layout.
2. **The package name**: `governed_agent` (proposed), or another.
3. **Shims or a hard switch**: shims (proposed) keep every old import working
   until a final clean-up PR; a hard switch is one large PR.
4. Whether `src/` (the Milestone-1 runtime) is folded in later or left as is
   (proposed: left as is; it is already a package and has its own tests).
