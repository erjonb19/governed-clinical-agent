# Governed Clinical Agent

A governed natural-language analytics agent for healthcare data. You ask a question in plain English; a language model writes the SQL; and a capability-based security runtime decides whether that SQL is allowed to run before it ever touches the database. The model proposes, the guardrails dispose.

It runs today over two real datasets — CMS hospital quality measures and a 367,000-resource FHIR clinical lakehouse — behind an authenticated HTTP API, with the full authorization decision returned in every response, and a 63-case ground-truth evaluation suite running in CI.

**Live:** https://governed-clinical-agent.onrender.com/docs

## Why this matters

Healthtech and care-navigation companies want to put an LLM in front of their data so staff and members can ask questions without writing SQL. The blocker is trust: a model can hallucinate a query, reach for a table it should never see, or be steered by a prompt injection into exfiltrating data. In healthcare that is not a bug, it is a compliance incident.

Most "AI agent for healthcare data" projects build the chatbot and bolt on governance later, if at all. This one is built the other way around. The governance is the product; the analytics agent is the proof that it works on real data. The guarantee is simple: the agent can only ever run a read-only, allow-listed, row-capped query against a de-identified view, no matter what the model is convinced to write.

And the claim is measured, not asserted — see [Evaluation](#evaluation).

## What it looks like

A natural-language request to `/query`:

```json
{ "question": "For heart failure care coordination, which in-network hospitals should we steer members to, balancing readmission performance against cost?" }
```

returns the SQL the model wrote, the SQL the guard actually ran, the decision, the rows, and what the call cost:

```json
{
  "allowed": true,
  "decided_by": "executed",
  "safe_sql": "SELECT facility_id, facility_name, state, readmit_hf, mspb_score FROM gold_hospital_profile WHERE ... ORDER BY readmit_hf ASC, facility_id ASC LIMIT 15",
  "row_count": 15,
  "rows": [ { "facility_name": "CHILTON MEDICAL CENTER", "state": "NJ", "readmit_hf": 15.8, "mspb_score": 1.14 } ],
  "planner_metrics": {
    "latency_ms": 800, "total_tokens": 650,
    "est_cost_usd": 0.000396, "model": "gpt-oss-120b", "provider": "cerebras"
  }
}
```

A disallowed query — even one that is perfectly valid SQL — is refused at the boundary:

```json
{ "allowed": false, "decided_by": "guard", "reason": "table not on Gold allowlist: bronze_patient" }
```

## Architecture

Two layers: a reusable security runtime, and applications built on top of it.

### The security runtime

Mediates every tool call an agent makes through a default-deny policy.

- **Policy engine** — risk-tiered, default-deny. Capabilities are autonomous, gated (require human approval), or denied outright. Defined in `medicare_policy.yaml`.
- **SQL guard** (`sql_guard.py`) — an AST validator (sqlglot) that is the enforcement seam for agent-written SQL. SELECT-only, table allowlist, automatic row cap, no stacked statements, no catalog/system schemas, no file-reading or catalog-introspection functions. Denies fail closed. Ships with an adversarial test harness.
- **Groundedness check** (`groundedness.py`) — verifies every claim in a generated brief traces back to a real returned row, so the agent cannot state a number it did not retrieve.
- **Taint tracking** (`src/security/`) — blocks data from a tainted source flowing into a denied sink; the defense against prompt-injection-driven exfiltration.
- **Audit logger** — every decision (allow / deny / require-approval) written to JSONL with capability, reason, and latency.

### The agent

- **NL-to-SQL planner** (`nl_to_sql_planner.py`) — turns plain English into one DuckDB SELECT. **Provider-agnostic** via an OpenAI-compatible client: Cerebras, Groq, and Anthropic are configured, switchable with one environment variable. Per-provider cost rates, because a frontier model runs ~7x an open model and a single global rate would misreport spend the moment you switch.
- **Stateful graph** (`agent_graph.py`) — LangGraph. The agent plans, executes through the guard, and on failure feeds the denial reason back into the next attempt and revises. **Bounded retries** (an unbounded agent loop is a real production failure mode). **Full trajectory capture** — every step, what it cost, how long it took.
- **Analytics tool** (`analytics_query_tool.py`) — runs the planner's SQL through the guard, then executes the validated query.
- **Backend-agnostic data layer** (`data_backends.py`) — local DuckDB or Databricks Delta behind one interface, selected by `DATA_BACKEND`. The governance is identical either way.

### Service and observability

- **HTTP service** (`app.py`) — FastAPI. `/health`, `/schema`, `/query` (natural language), `/raw-sql` (guarded SQL), `/metrics` (aggregate cost and latency). API-key auth on the data routes.
- **Cost tracking** (`cost_tracker.py`) — per-call token counts and estimated cost in every response; aggregate totals, averages, and latency p50/p95 at `/metrics`. Percentiles rather than averages, because the average hides the slow tail.
- **AIOps panel** (`aiops_panel.py`) — Streamlit over the audit logs: outcomes by control, denial reasons, latency, decisions over time.

## The data

Two lakehouses, queried through identical guardrails.

### CMS hospital quality

Built from the CMS Provider Data Catalog with a two-catalog fetcher (`fetch_cms.py`) and a DuckDB medallion pipeline (`build_hospital_gold.py`).

`gold_hospital_profile` — ~750 hospitals across twelve Northeast and Mid-Atlantic states (the exact count changes with each monthly CMS refresh), joined on CMS facility ID: overall star rating, Medicare spending per beneficiary, five condition-level 30-day readmission rates, and four ED-flow measures including median psychiatric ED boarding time. A geographic Gold (region utilization, cost, anomaly) is built by `build_medallion.py`.

### FHIR clinical lakehouse

**367,296 resources** flattened from 1,180 nested Synthea FHIR R4 bundles (`fetch_synthea.py`, `build_fhir_gold.py`).

| Bronze (faithful FHIR) | rows | Gold (curated) | rows |
|---|---|---|---|
| `bronze_patient` | 1,180 | `gold_patient` | 1,180 |
| `bronze_encounter` | 46,868 | `gold_encounter` | 46,868 |
| `bronze_condition` | 8,766 | `gold_condition` | 8,766 |
| `bronze_observation` | 259,929 | `gold_observation` | 223,503 |
| `bronze_procedure` | 36,451 | `gold_procedure` | 36,451 |
| `bronze_medication_request` | 14,102 | `gold_medication` | 14,102 |

Coded in **SNOMED** (conditions, procedures), **LOINC** (observations), and **RxNorm** (medications).

The engineering is in the flattening, not the data. Real bulk FHIR is deeply nested JSON, and each of these exists because the naive version breaks on it:

- **Reference normalization** — references appear as both `urn:uuid:abc` and `Patient/abc` in the same export. Without normalizing both, half the joins silently miss.
- **CodeableConcept lifting** — a diagnosis is never a string; it is `code.coding[0].{system,code,display}` with a `text` fallback, and `coding` may be absent or empty.
- **Polymorphic values** — `Observation.value` is `valueQuantity`, `valueCodeableConcept`, or `valueString`. Real exports mix all three in one table.
- **Varying cardinality** — `name`, `address`, `category` may be absent, empty, or multiple.
- **Data-quality gates** — deduplication on primary keys before joining, plus a **fan-out gate** asserting no Gold table exceeds its Bronze source. A duplicate ID silently multiplies rows and corrupts every downstream count; this catches it loudly.

### The warehouse remembers

CMS republishes hospital quality measures monthly, and `data-refresh.yml` rebuilds
the Gold on the 1st. That rebuild used to delete the database first, so every
refresh discarded the previous reading: the warehouse could say what a hospital's
readmission rate **is** and never what it **was**, despite having been fed the
data to answer that.

`gold_hospital_history` is a **Type 2 slowly changing dimension** beside the
current profile. Nothing is updated in place — a changed row is closed and a new
version opened:

| facility_id | star_rating | valid_from | valid_to | is_current |
|---|---|---|---|---|
| 330101 | 3.0 | 2026-09-01 | 2026-11-01 | false |
| 330101 | 4.0 | 2026-11-01 | *null* | true |

`valid_to` is **exclusive**, so a version is true for `[valid_from, valid_to)`.
Consecutive versions share a boundary date without overlapping, and "as of date
D" is one predicate with no edge case at the changeover:

```sql
WHERE valid_from <= D AND (valid_to IS NULL OR valid_to > D)
```

Three details that decide whether this works:

- **Change detection is a row hash, read from the database schema.** A hardcoded
  column list is the kind of thing that silently stops tracking a new measure,
  and nobody notices until they ask why the history looks flat.
- **NULLs are normalised to a sentinel before hashing.** `concat_ws` *skips*
  null arguments, so `('x', NULL)` and `(NULL, 'x')` would otherwise hash
  identically — two different rows, one hash, a real change recorded as none.
- **Out-of-order vintages are refused.** Loading an older snapshot after a newer
  one opens a version *before* the one it supersedes, and every "as of" query
  then matches two rows. Better to fail the load than to repair it later.

Every build runs `gold_history.verify()` and fails on violation — duplicate
current rows, backwards intervals, overlapping versions. A drifted Type 2 table
still answers queries, it just answers them wrongly, and nothing about the result
looks suspicious.

**Not yet exposed to the agent.** The table holds one vintage today, so trend
questions have nothing to compare against; it goes on the guard allowlist and
into `SCHEMA_DOC` once the next monthly refresh gives it a second one.

### PHI protection

Only the curated **Gold** views are on the guard's allowlist. The `bronze_*` tables — which carry raw patient names, addresses, and identifiers — are deliberately excluded, even though they sit in the same database. This is the analytics equivalent of querying a de-identified view instead of the source system.

Tested adversarially, including the case that matters most:

```sql
-- starts in an ALLOWED table, joins to a DENIED one to re-identify patients
SELECT g.patient_id, b.family_name
FROM gold_condition g JOIN bronze_patient b ON g.patient_id = b.patient_id
-- DENIED: table not on Gold allowlist: bronze_patient
```

A guard that only checked the first table reference would let that through. This one walks every table in the parse tree.

## Evaluation

The correctness claim is measured against ground truth, not asserted.

**63 questions across two datasets**, each paired with hand-written reference SQL:
`eval_bank.py` (35 cases, CMS hospital quality) and `eval_bank_fhir.py` (28 cases,
FHIR clinical). Both span five difficulty tiers, from simple aggregates to
cross-table clinical reasoning.

`eval_harness.py` runs every question **three times** — LLM output is
non-deterministic, and a case that passes 1 of 3 is not passing — scores the
agent's answer against the reference for **exact match**, classifies failures
into a taxonomy, and writes a timestamped report for regression tracking.

**Measured results — single-shot vs. self-correcting graph.** Latest full sweep,
3 runs per case against HEAD on Gemini `gemini-flash-lite-latest`: all four
suites, 63 cases, 378 runs, **not one incorrect answer and no provider errors**.

| | CMS hospital (35 cases) | FHIR clinical (28 cases) |
|---|---|---|
| Single-shot | 35/35 cases · 100% runs | 28/28 cases · 100% runs |
| Graph (self-correcting) | 35/35 cases · 100% runs | 28/28 cases · 100% runs |

Every tier is at 100% in all four reports. The three hospital/FHIR single-shot
and hospital graph runs are from 2026-09-05; the FHIR graph run is 2026-09-07,
delayed two days because the Gemini free tier's 500-requests-per-day-per-model
cap was exhausted — three attempts returned `provider_unavailable` and are not
reported as results, since an inconclusive run is not a measurement.

**What the current sweep does and does not show.** In the August sweeps
(`eval_single_20260803T030757Z`, 33/35 · 95%; `eval_graph_20260803T033040Z`,
35/35 · 99%) the graph path beat single-shot, and the whole difference was
transient tool failures that retry recovered. At HEAD that gap is gone — not
because self-correction regressed, but because **single-shot no longer produces
the failures it used to recover**. Both graph runs report zero retries —
`0/35` and `0/28` cases, avg attempts 1.00: the retry loop was never entered, so
this sweep is evidence neither for nor against it.

The mechanism is still the point, and it predicts when retry can help at all.
Retry only helps when there is a *failure signal* to react to. A guard denial
produces one — the reason is fed back into the next attempt and the agent
revises. A query that is wrong but valid produces none: it runs, returns rows,
and looks successful. In the August runs every failure self-correction recovered
was a transient denial, and every failure it could not recover was a
wrong-but-successful query. That asymmetry is why the ground-truth suite exists
alongside the retry loop rather than being replaced by it.

Some cases exist specifically to test clinical correctness, not just SQL
correctness. A pair of them ask the same question at different grains — how many
*records* mention viral sinusitis (1,237) versus how many *distinct patients*
have it (738). Conflating those is the most common error in clinical analytics;
the agent gets both right, every run.

Two defects the eval framework found that inspection did not: a scoring bug in
the harness itself, and non-deterministic ranking in the agent — fixed by
requiring a deterministic tie-break, which took tier-3 accuracy from 71% to 100%.

```bash
python eval_harness.py                              # hospital, single-shot
python eval_harness.py --graph                      # hospital, self-correcting
$env:EVAL_DATASET="fhir"; python eval_harness.py    # clinical
python eval_harness.py --provider groq              # fallback when primary is down
python validate_fhir_bank.py                        # verify the answer key itself
```

Every run above is committed under `eval_runs/`:
`eval_single_hospital_20260905T180442Z.json`,
`eval_graph_hospital_20260905T181329Z.json`,
`eval_single_fhir_20260905T182030Z.json`, and
`eval_graph_fhir_20260907T011508Z.json`.

**Infrastructure failures vs. accuracy regressions.** The harness distinguishes
the two so a provider outage never looks like the agent getting worse. A run that
fails with a provider/infra error — payment (402), rate limit (429), 5xx, network,
or timeout — never reached the model, so it is excluded from the accuracy
denominator rather than counted as a wrong answer. Accuracy alone is not enough
though: a verdict computed from a handful of runs that happened to get through
would be confident and meaningless. So the gate also requires that at least
`MIN_COMPLETED_FRACTION` (80%) of planned runs actually completed. Below that, the
run is reported as **provider unavailable** and exits with a distinct code, rather
than as a false accuracy regression — or a false pass. A provider that is plainly
down is also detected early: after `ABORT_AFTER_CONSECUTIVE_PROVIDER_ERRORS` (12)
consecutive infrastructure failures the remaining runs are recorded without being
attempted, so a dead provider costs about a minute instead of a full sweep's
worth of pacing delay:

| exit code | meaning |
|---|---|
| `0` | pass (accuracy ≥ threshold) |
| `1` | accuracy regression — a real failure to investigate |
| `2` | provider unavailable — inconclusive, not a regression |

When the primary provider is down, re-run against a **fallback provider** with
`--provider` (or `PLANNER_PROVIDER`); it needs that provider's API key set:

```bash
python eval_harness.py --provider gemini      # needs GEMINI_API_KEY (free tier)
python eval_harness.py --provider groq        # needs GROQ_API_KEY
python eval_harness.py --provider xai         # needs XAI_API_KEY (xAI/Grok)
python eval_harness.py --provider anthropic   # needs ANTHROPIC_API_KEY
```

For rate-limited free/low tiers, pace calls with `--min-interval <seconds>` (or
`PLANNER_MIN_INTERVAL_SEC`) to avoid tripping 429s across a full sweep.

## CI

Four GitHub Actions workflows:

- **Tests on every push/PR** (`tests.yml`) — the full unit / integration / adversarial-security suite, **430 tests, no deselects**. No API keys needed; only the opt-in `network` marker is skipped.
- **Quick eval gate on every push/PR** (`eval-on-push.yml`) — a 6-case subset spanning all tiers, ~90 seconds. Fails the build if accuracy drops below threshold.
- **Full eval nightly** (`eval-nightly.yml`) — two jobs: all 35 hospital cases, then all 28 FHIR cases (building its Gold from Synthea, cached), 3 runs each.
- **Monthly data refresh** (`data-refresh.yml`) — re-fetches the CMS sources, rebuilds the hospital Gold, **validates it with the eval gate before committing**, and stamps `medallion/REFRESH.json`. A regression (exit 1) blocks the commit; a provider outage (exit 2) does not.

Fast feedback on every change, complete coverage every night. Reports are uploaded as artifacts.

## Run it

```bash
pip install -r requirements-api.txt

$env:PLANNER_PROVIDER="gemini"  # what CI and both eval suites run on
$env:GEMINI_API_KEY="..."       # the planner's model (free key at aistudio.google.com)
$env:API_KEY="..."              # require a key on the data endpoints

uvicorn app:app --port 8000     # http://localhost:8000/docs
```

Then open **http://localhost:8000/ui** — a self-contained web UI to ask questions
(or run SQL) and see the decision, the SQL the guard ran, the rows, and the cost.
The API's Swagger docs remain at `/docs`.

The UI has six views. Most are read-only, but **Review** is the human-in-the-loop
surface: it shows the approval queue, drafts a proposal into it (`/propose`), and
submits any of the four decisions — approve, reject, escalate, approve with edits
— which **resumes the paused agent** on the server. Queue depth, average wait,
approval rate, and escalation rate are shown alongside, because a queue nobody
drains is a governance control that exists only on paper.

To run against Databricks instead of local DuckDB (**verified** on Databricks
Free Edition serverless SQL; needs `pip install databricks-sql-connector`). First
publish the Gold into Delta (full setup is in the script header):

```bash
python scripts/publish_gold_to_databricks.py --db medallion/hospital_gold.duckdb
# --dry-run previews the SQL without connecting; --max-rows N does a smoke load

$env:DATA_BACKEND="databricks"
$env:DATABRICKS_SERVER_HOSTNAME="..."; $env:DATABRICKS_HTTP_PATH="..."; $env:DATABRICKS_TOKEN="..."
```

Other entry points:

```bash
python agent_graph.py              # self-correcting agent demo, with trajectory
python eval_harness.py             # the 35-case ground-truth suite
python sql_guard.py                # adversarial guard harness (instant, no API calls)
streamlit run aiops_panel.py       # observability dashboard
```

Switching provider or dataset:

```bash
$env:PLANNER_PROVIDER="gemini"      # gemini | groq | cerebras | xai | anthropic
$env:PLANNER_SCHEMA="fhir"          # hospital | fhir
$env:PLANNER_MIN_INTERVAL_SEC="12"  # pace calls for free/low-tier rate limits (Groq free tier ~8K TPM)
```

Note: `xai` is xAI/Grok (`api.x.ai`, `XAI_API_KEY` starting `xai-`), a different
vendor from `groq` (`api.groq.com`, `GROQ_API_KEY` starting `gsk_`).

Rebuilding the data (both are derived artifacts, not committed):

```bash
python fetch_cms.py xubh-q36u hospital_general   # (and the other four CMS files)
python build_hospital_gold.py

python fetch_synthea.py                          # ~85MB of FHIR bundles
python build_fhir_gold.py                        # ~367k resources
```

## Scope and honest limitations

A working reference implementation, described plainly:

- Single-instance; queries serialized under a lock. Horizontal scaling is future work.
- Auth is enforced when `API_KEY` is set, and the service runs **open in dev mode** when it is not (flagged loudly at startup and in `/health`). There is **no rate limiting** yet.
- The data is public CMS data and **synthetic** Synthea patients. There is no real PHI here, and the system is not hardened for PHI or production load. The PHI-protection design is real and tested; the deployment posture is not production-grade.
- The **hospital** Gold refreshes monthly in CI; the FHIR Gold is a fixed Sep-2019
  Synthea sample and is built on demand (it is a gitignored derived artifact, so
  the deployed instance serves the hospital dataset only).
- Cost figures are **estimates** (tokens × a configurable rate), not billing data.

## Roadmap

**Done**

- Human-in-the-loop approval queue as a first-class graph node — LangGraph
  `interrupt` + SQLite checkpointer, four decision types, **and a review UI**.
  Escalation is a real handoff: the item stays queued under its new owner and
  the agent stays paused, so whoever inherits it can still approve it and have
  the action run. Every hop is kept in an `approval_events` chain.
- The analytics capability is exposed over MCP — `query_analytics` and
  `analytics_schema` in `mcp_server.py`, mediated by the same policy engine and
  the same `sql_guard`. The client's model writes the SQL; this server is the
  part that cannot be talked out of its rules.
- Cloud lift for Databricks SQL — Gold published to Delta, eval subset passes
  100% on both backends (parity proven). ADLS Gen2 + Databricks Workflows
  orchestration is still future work.
- Scheduled monthly data refresh — `data-refresh.yml`, gated on the eval suite so
  changed source data cannot silently ship.
- Rate limiting and fail-closed auth — per-API-key limits via slowapi; the service
  refuses to start without `API_KEY` unless open access is set explicitly.

**Next**

- RAG over CMS measure definitions, so the agent can answer *what a measure means*, not only what its value is
- MCP server Milestone 2+: a general capability-mapping layer, then Streamable HTTP and OAuth 2.1 (currently stdio)
- Measure the self-correcting graph against single-shot on the full suite

## License

MIT.
