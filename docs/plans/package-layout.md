# Plan: one package with layers (backlog item 18)

**Status: plan only, waiting for the owner's OK (written 2026-11-09, brought up to date
2026-11-25).** Nothing has moved. The 2026-11-25 update maps the 13 modules added since
(item 19's `state_db.py`, item 22's `user_keys.py`, item 39's `audit_store.py`, ...),
corrects one wrong claim (pydantic-settings is in no lockfile; see Settings) and adds the
`Dockerfile` and the commands in workflows and `docs/OWNER_STEPS.md` to "What must survive".

## Where things are today (counted 2026-11-25 on `main`)

- 60 Python modules at the repo root (plus `setup.py`), beside `src/` (the Milestone-1 runtime:
  `src/runtime`, `src/security`, `src/policies`, `src/tools`, `src/utils`).
  `setup.py` packages `src` (`find_packages()`), but nothing at the root, so the
  root modules are only importable from a checkout with the repo on `sys.path`;
  most of them do `sys.path.insert(0, ...)`.
- 70 distinct environment variables read with `os.environ`/`os.getenv` across 30 root
  modules (a few CI-only, such as `GITHUB_*`), several read in more than one place and
  some at **import time** (`nl_to_sql_planner` binds `SCHEMA_DOC`/`SYSTEM_PROMPT` from
  `PLANNER_SCHEMA` when imported, which is why `eval_harness.py` must assign it before
  importing).
- `app.py` builds the app at import (`app = FastAPI(...)`), with its state in a
  module-level `_STATE` dict; tests reload the module to get a fresh app.
- Root scripts are run by name from workflows and owner steps: `user_keys.py` (owner step
  15), `fetch_synthea.py`, `fetch_cms.py` (run by `data-refresh.yml` through `subprocess`), `build_fhir_gold.py`, `build_hospital_gold.py`, `eval_harness.py`,
  `scheduled_runs.py`, `nightly_alerts.py`, `slo.py`, `security_alerts.py`, `live_check.py`,
  `deploy_lag.py`; and `python sql_guard.py` (the adversarial harness, run by hand per CLAUDE.md).
  `tests.yml` also runs `mypy` on four files by path (`critic.py`, `nightly_alerts.py`,
  `sql_guard.py`, `phi_guard.py`).

## Target layout

```
governed_agent/                 # the one package (name open: see "Decisions")
  api/          app factory (create_app), routes, the /v1 prefix, auth, rate limits
                  <- app.py, findings.py, mcp_server.py, auth_failures.py,
                     user_keys.py, feedback.py, question_screen.py
  agent/        planner, critic, graphs, approvals, replay, model cache and budget
                  <- nl_to_sql_planner.py, critic.py, agent_graph.py,
                     approval.py, approval_graph.py, replay.py, versioning.py,
                     llm_cache.py, llm_budget.py, groundedness.py,
                     brief_commit_tool.py, region_context_tool.py
  guard/        sql_guard.py, phi_guard.py, phi_columns.yaml, schema_check.py
                  (moved LAST and ALONE, see "Order")
  data/         backends, the analytics tool, state store, Gold builders and quality gates
                  <- data_backends.py, analytics_query_tool.py, state_db.py,
                     data_quality.py, gold_history.py, build_fhir_gold.py,
                     build_hospital_gold.py, build_medallion.py, fetch_cms.py,
                     fetch_synthea.py
  evals/        harness, banks, trajectory scoring, scheduled runs
                  <- eval_harness.py, eval_bank.py, eval_bank_fhir.py,
                     eval_bank_adversarial.py, trajectory_eval.py,
                     scheduled_runs.py, validate_fhir_bank.py, validate_results.py
  observability/ tracing, export, cost, metrics, audit store, free-text redaction, alerts
                  <- tracing.py, trace_export.py, cost_tracker.py, aiops_metrics.py,
                     audit_store.py, phi_redact.py, slo.py, nightly_alerts.py,
                     security_alerts.py, deploy_lag.py
  settings.py   ONE typed settings object, read once (see Settings)
src/            the Milestone-1 runtime, unchanged (it already is a package)
stay at the root (entry points and demos, not library code; they import the package):
  demo_*.py (4), aiops_panel.py, aiops_demo_run.py, live_check.py, peek_grain.py,
  peek_measures.py, run_hospital_query.py, setup.py; scripts/ stays as it is
```

That places all 60 root modules: 7 api, 12 agent, 3 guard (+ the YAML), 10 data, 8 evals,
10 observability, 10 stay. `phi_redact.py` goes to observability, not guard: it redacts logs,
not SQL, and should not ride with the guard's last-and-alone PR. `question_screen.py`
(refuses a question with an identifier before any model call) goes to api for the same reason.
Every script that a workflow or owner step runs by name keeps a root file of that name (a
shim, below), so no workflow or `docs/OWNER_STEPS.md` command changes until step 5. A shim
run as a script must also run the real module's entry point: replacing `sys.modules` does
not run the moved module's `if __name__ == "__main__"` block, so such shims end with
`if __name__ == "__main__": sys.exit(<real module>.main())` (a test runs each by name with
`--help` or its dry-run flag).

### Settings

A `Settings` class with every variable, its type, default and one-line meaning, in
groups (planner, data, auth, tracing, approvals, evals). **Correction (2026-11-25):** the
first version of this plan said pydantic-settings was already installed through `mcp`; it
is not (`mcp` 2.3.0 does not pull it in, and no lockfile lists it; a developer machine may have it installed by hand), so it would be a new
dependency (directive 11). Proposed instead: a plain `pydantic.BaseModel` (pydantic 2.14 is
already pinned) filled by one small `from_env()` that reads each field's variable, so no
new package; pydantic-settings stays an option if the owner prefers it (decision 5).
Modules take the values from `get_settings()` at **call** time, not import time, so tests
set them per test instead of reloading modules. The README's configuration table is
generated from it, so docs and code cannot drift.

### App factory

`create_app(settings=None) -> FastAPI` replaces the module-level app: the
lifespan builds the runtime, planner, graphs and tracer from the settings, and
`app.py` keeps `app = create_app()` so `uvicorn app:app` -- Render's start command
(`render.yaml`) and the API `Dockerfile`'s `CMD` -- does not change, and neither file
is edited by this work.

## Order (each step one small PR, no behaviour change)

1. `settings.py` + `create_app`, with the current modules reading from them;
   no file moves yet. The full suite, the replayed push gate and the E2Es green.
2. `observability/` (no dependants inside the guard).
3. `data/`, then `agent/`, then `evals/`, then `api/`. Each move leaves a
   **shim** at the old path. A plain `import *` is not enough: a test that
   patches `critic._something` or reads module-level state must reach the real
   module, so the shim is the module itself (`sys.modules[__name__] =
   governed_agent.agent.critic`). Every existing import, patch, script, workflow
   and doc keeps working.
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
- Render's start command and the `Dockerfile` `CMD` (both `uvicorn app:app`), and every
  workflow, unchanged until step 5; `render.yaml` is not edited at all (a Render change
  needs the owner's OK).
- Every command a workflow or `docs/OWNER_STEPS.md` runs by file name (listed above),
  through the shims, until step 5 updates them in the same PR that removes the shims.
- The type check: `tests.yml`'s `mypy` step moves to the new paths in the same PR that moves
  each of its files (a shim would leave mypy checking the shim, not the code), and the guard
  files keep `mypy --strict`.
- The state stores (item 19: `STATE_DB_URL`, Postgres or SQLite) and the audit hash chain
  (item 39): table names and schemas do not change, so a running deploy's data stays
  readable across every step.
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
5. **Settings without a new dependency** (proposed: a plain pydantic model with one
   `from_env()`), or add pydantic-settings (a new dependency, needs your OK).

Answering 1 to 5 is all this plan needs; step 1 can start the next working day after.
