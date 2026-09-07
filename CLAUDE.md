# CLAUDE.md — Security-Constrained-Agent-Runtime

## What this project is
A governed agentic AI runtime: a FastAPI NL-to-SQL service with a security guard layer, plus a Guarded MCP server (FastMCP) built on top. The planner is a LangGraph state graph — `plan` and `execute` nodes plus a `finish` node, with a conditional route edge out of `execute` (`_route` → back to `plan` or on to `finish`) that drives bounded self-correction: guard denials feed back into retries, with a retry cap.

> MCP server status: `mcp_server.py` is Milestone 1 — FastMCP over **stdio**, mediating every tool call through the real policy engine (`execute_tool`). Streamable HTTP + OAuth 2.1 are planned (docs/plan.md, docs/DESIGN.md), **not yet implemented** — do not claim they work.

Deployed at: https://governed-clinical-agent.onrender.com (API-key auth).
Repo: github.com/erjonb19/Security-Constrained-Agent-Runtime

## Architecture
- **API layer:** FastAPI service, API-key auth
- **Planner:** LangGraph state graph with bounded self-correction; multi-provider LLM abstraction selectable via `PLANNER_PROVIDER` (Gemini, Cerebras gpt-oss-120b, Groq gpt-oss-120b, xAI/Grok, Anthropic). **CI and both eval suites run on Gemini** (`gemini-flash-lite-latest`, free tier) — see Validation below; the other providers stay selectable as fallbacks. Note: if `PLANNER_PROVIDER` is unset the code falls back to `cerebras` (`nl_to_sql_planner.py`), so set it explicitly. Notes: Groq's llama-3.3-70b-versatile is deprecated — do not reintroduce it. `xai` (`api.x.ai`, `XAI_API_KEY` starting `xai-`) is a *different vendor* from `groq` (`api.groq.com`, `GROQ_API_KEY` starting `gsk_`); don't conflate them.
- **Guard layer:** validates all generated SQL before execution. SELECT-only, table allow-list, row caps, PHI redaction.
- **Data layer:** backend-agnostic — LocalDuckDBBackend and DatabricksBackend. The query path (`AnalyticsQueryTool`) now routes execution through the selected backend (`DATA_BACKEND=databricks` switches to Delta); the guard still validates SQL first. The Databricks path is **VERIFIED** against a real workspace (Free Edition serverless SQL): gold published to `workspace.gold` Delta, agent queries served with the guard intact (a catalog query is still DENIED on that path), and the 6-case eval subset passed 100% for backend parity.
- **Web UI:** `web/index.html` served at `/ui` (root `/` redirects there) — a self-contained page calling `/query` and `/raw-sql` with `X-API-Key`. Six views: Ask, **Review** (the approval queue), Data, Guide, Trust, Usage. The Review view drives `/propose`, `/approvals`, and `/approvals/{id}`, and is the human-in-the-loop surface — it is the only place the four decision types (approve / reject / escalate / approve_with_edits) are exercised outside tests.
- **Data:** CMS hospital-quality lakehouse in DuckDB (~750 hospitals, 12 states; the exact count moves with each monthly CMS refresh -- do not hardcode it) + FHIR lakehouse from 1,180 Synthea R4 bundles, medallion architecture
- **Human-in-the-loop:** approval checkpoint with SQLite persistence
- **Cost/latency tracking:** per-call and aggregate

## Hard rules — never violate
1. **Never bypass or weaken the guard layer.** All SQL goes through the validator. No raw execution paths, no debug backdoors left in code.
2. **SELECT-only** against data backends. No DDL/DML from the agent path.
3. **PHI protection stays intact.** Redaction logic must not be removed or short-circuited, even in tests.
4. **No real credentials or API keys in code, tests, or fixtures.** Env vars only.
5. **Push to the `myfork` remote**, never directly to upstream main.
6. **Schema and planner must agree.** Any change to database schema requires updating PLANNER_SCHEMA (and `SCHEMA_DOC` in `nl_to_sql_planner.py`) in the same commit — mismatch causes silent failures. The schema validator now exists (`schema_check.py`) and runs at graph startup (`agent_graph.py:103`, strict); exercise it by constructing the graph, e.g. `python eval_harness.py --graph` or `python agent_graph.py`. It raises on zero overlap and warns on partial overlap.

## Validation — required before reporting any task done
1. Run the full test suite: `pytest` (config in `pytest.ini`; runs `tests/` — `unit/`, `security/`, `integration/`, 29 files). Requires dev deps: `pip install -e ".[dev]"`. Quick guard-only check with no API calls: `python sql_guard.py` (adversarial harness).
2. Run both eval suites. Each needs `GEMINI_API_KEY` set (the provider CI and both suites use) and the Gold DBs built (`medallion/hospital_gold.duckdb`, `medallion/fhir_gold.duckdb`):
   - 35-case hospital eval: `python eval_harness.py`
   - 28-case FHIR eval: `$env:EVAL_DATASET="fhir"; python eval_harness.py`  (PowerShell; bash: `EVAL_DATASET=fhir python eval_harness.py`)
   - Add `--graph` to run through the self-correcting LangGraph path instead of single-shot. Env knobs: `EVAL_RUNS=N`, `EVAL_SUBSET=1` (quick tagged subset). Gate threshold is accuracy over **completed** runs ≥ 80% (`THRESHOLD` in `eval_harness.py`); runs that never reached the provider are excluded from the denominator, and if fewer than `MIN_COMPLETED_FRACTION` (80%) of planned runs completed, the verdict is `provider_unavailable` (exit 2) rather than a pass/fail computed from a thin sample. The harness now forces `PLANNER_SCHEMA` from `EVAL_DATASET` before importing the planner, so the FHIR command above is correct as written even with a `.env` that pins `PLANNER_SCHEMA=hospital`.
   - **Exit codes distinguish infra from regression:** `0` pass, `1` accuracy regression (real — investigate), `2` provider unavailable (inconclusive — the LLM provider returned 402/429/5xx for most runs; NOT a regression). Don't "fix" a code-2 run by touching eval cases — it means the provider was down.
   - **Provider + pacing:** CI evals, the eval suites, and the Render deploy all run on **Gemini** (`gemini-flash-lite-latest`, free tier), paced with `PLANNER_MIN_INTERVAL_SEC=5`. `--provider {gemini,cerebras,groq,xai,anthropic}` (or `PLANNER_PROVIDER`) switches, each needing that provider's API key; use `--min-interval <sec>` locally. Groq remains a configured fallback -- its free tier is rate-limited (~30 RPM / 8K TPM / 1K RPD) and 429s under a full sweep, which is why it is no longer the default. Do not reintroduce the deprecated Groq `llama-3.3-70b-versatile` model.
3. All evals must pass ground-truth validation. If a case fails, fix or explain — never lower the threshold or delete the case to pass.
4. CI runs three workflows: `tests.yml` (pytest on push/PR — the **full** unit/integration/security suite, 376 passing, **no deselects**; only the opt-in `network` marker is skipped), `eval-on-push.yml` (6-case hospital gate), and `eval-nightly.yml` (two jobs: 35-case hospital + 28-case FHIR, 3 runs each, the latter building its Gold from Synthea with a cache). A task is not complete if any of these would fail. Evals run on **Gemini** (`gemini-flash-lite-latest`, free tier, `PLANNER_MIN_INTERVAL_SEC=5`) -- verified **63/63 cases at 100%** (35/35 hospital + 28/28 FHIR). Groq's free tier 429s under full load; groq/cerebras/xai/anthropic remain selectable.

## Development workflow
- PR-driven: changes go through a branch and PR, not direct commits to main
- Small, reviewable diffs preferred over large rewrites
- When adding features, mirror existing patterns (guard checks, eval cases, cost tracking hooks) rather than inventing new structures
- Every new capability gets eval cases added in the same PR

## Current roadmap (in priority order)
1. Reframe demo questions as provider workflows
2. Measure graph vs single-shot performance using the eval suite
3. RAG + vector retrieval over FHIR data
4. MCP server: Streamable HTTP transport + OAuth 2.1 (currently stdio, Milestone 1). Milestone 2 (the capability *mapping* layer, so tools stop hardcoding their capability) is the next unblocked step and needs no external decisions.
5. Expanded README

Done:
- Schema validator (`schema_check.py`, catches PLANNER_SCHEMA/database drift — wired into graph startup).
- Databricks lift — VERIFIED end-to-end on a real workspace, backend parity proven.
- **Approval queue UI** — `web/index.html` "Review" view. The backend (`approval.py`, `approval_graph.py`, `/propose` + `/approvals*`) had been complete and unreachable from the UI; it is now driveable end to end, including the reviewer identity recorded on every decision.

## Conventions
- **`demo_*.py` at the repo root are narrative demo scripts, not tests.** They have a `main()`, print a story, and are NOT collected by pytest (`pytest.ini` sets `testpaths = tests`). They were once named `test_*.py`, which made them look like orphaned tests. Real tests live only under `tests/`.
- Python; follow existing code style in the repo
- DuckDB patterns already in use: memory caps and disk spill for large operations — keep them
- Config via environment variables, documented in README when added
