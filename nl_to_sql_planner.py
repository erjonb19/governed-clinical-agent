"""
nl_to_sql_planner.py
====================
The piece that turns this from a dashboard into an agent. It takes a plain
English question, asks an LLM to write SQL against the Gold schema, then hands
that SQL to your governed runtime. The model PROPOSES; sql_guard DISPOSES.

This is the point of the whole project: the planner can generate any SQL it
wants, including unsafe SQL, and the guard still only lets read-only, allowlisted,
row-capped queries through. The governance constrains the model, not your own
hand-written queries.

Provider-agnostic by design. Both Cerebras and Groq expose OpenAI-compatible
endpoints, so switching providers is two lines in PROVIDERS / ACTIVE_PROVIDER.

Setup:
    pip install openai
    setx CEREBRAS_API_KEY "csk-..."     (PowerShell: then reopen the shell)

Run the end-to-end demo (needs medallion\\hospital_gold.duckdb + the runtime):
    python nl_to_sql_planner.py
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load a local .env (if present) before reading os.environ -- so provider keys and
# PLANNER_* settings work in local/CLI runs. Optional dependency; never hard-fail.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from openai import OpenAI


# ---------------------------------------------------------------------------
# Provider config. Switch ACTIVE_PROVIDER to move between vendors. One line.
# ---------------------------------------------------------------------------
PROVIDERS = {
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "api_key_env": "CEREBRAS_API_KEY",
        "model": "gpt-oss-120b",   # available on this account; also: zai-glm-4.7
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        # Groq namespaces the OpenAI open-weight models under "openai/"; the bare
        # "gpt-oss-120b" 404s. Do NOT reintroduce llama-3.3-70b-versatile (deprecated).
        "model": "openai/gpt-oss-120b",
    },
    # xAI (Grok) -- another OpenAI-SDK-compatible endpoint. NOTE: this is xAI
    # (api.x.ai), a DIFFERENT vendor from Groq (api.groq.com). Its key is
    # XAI_API_KEY and starts with "xai-". xAI does NOT host gpt-oss-120b, so this
    # uses a Grok model; override the exact model id with $XAI_MODEL if needed.
    "xai": {
        "base_url": "https://api.x.ai/v1",
        "api_key_env": "XAI_API_KEY",
        "model": os.environ.get("XAI_MODEL", "grok-3"),
    },
    # Anthropic exposes an OpenAI-SDK-compatible endpoint, so Claude drops into
    # the SAME abstraction with no client-code changes -- the payoff of building
    # this provider-agnostic from the start. This is the frontier-model option
    # for harder or more ambiguous questions, where the fast open models can be
    # underspecified.
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1/",
        "api_key_env": "ANTHROPIC_API_KEY",
        "model": "claude-sonnet-4-5",
    },
    # Google Gemini via its OpenAI-compatible endpoint. Its FREE tier is far more
    # generous than Groq's (much higher tokens-per-minute), so it can run the full
    # 63-case eval suite without the 429 throttling the Groq free tier hits. Get a
    # free key at aistudio.google.com. Override the model with $GEMINI_MODEL.
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GEMINI_API_KEY",
        "model": os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest"),
    },
}
# One line to switch providers, or set PLANNER_PROVIDER in the environment.
# The guard, policy, eval suite, and API are all unchanged by this choice --
# governance holds regardless of which model writes the SQL.
ACTIVE_PROVIDER = os.environ.get("PLANNER_PROVIDER", "cerebras")

# Cost is tokens x rate. Tokens are ground truth and never go stale; the RATE
# is the volatile part (providers change prices -- Cerebras changed tiers in
# July 2026), so it lives in ONE place. Update this, not the code, when pricing
# moves. USD per 1M tokens, a labeled ESTIMATE, not a bill.
#
# Rates are PER PROVIDER: a frontier model costs materially more than a fast
# open model, so a single global rate would silently misreport cost the moment
# you switch backends.
PROVIDER_RATES = {
    "cerebras":  {"input": 0.85, "output": 0.85},
    "groq":      {"input": 0.59, "output": 0.79},
    "anthropic": {"input": 3.00, "output": 15.00},
    "xai":       {"input": 3.00, "output": 15.00},   # grok-3 estimate
    "gemini":    {"input": 0.15, "output": 0.60},    # gemini-2.5-flash est; free tier = $0
}
_DEFAULT_RATE = {"input": 1.00, "output": 1.00}


# --- optional call pacing --------------------------------------------------
# Free/low tiers cap requests-per-minute and tokens-per-minute. When
# PLANNER_MIN_INTERVAL_SEC is set, enforce at least that many seconds between
# planner API calls (process-wide) so an eval sweep does not trip 429s. It lives
# at the planner so it also paces the graph's self-correction retries, not just
# the top-level eval loop.
_pace_lock = threading.Lock()
_last_call_ts = [0.0]


def _pace_call() -> None:
    try:
        interval = float(os.environ.get("PLANNER_MIN_INTERVAL_SEC", "0") or 0)
    except ValueError:
        interval = 0.0
    if interval <= 0:
        return
    with _pace_lock:
        wait = interval - (time.monotonic() - _last_call_ts[0])
        if wait > 0:
            time.sleep(wait)
        _last_call_ts[0] = time.monotonic()


class CallMetrics:
    """Per-call cost and latency for one model invocation."""
    def __init__(self, latency_ms, prompt_tokens, completion_tokens, model, provider):
        self.latency_ms = latency_ms
        self.prompt_tokens = prompt_tokens or 0
        self.completion_tokens = completion_tokens or 0
        self.total_tokens = self.prompt_tokens + self.completion_tokens
        self.model = model
        self.provider = provider
        rate = PROVIDER_RATES.get(provider, _DEFAULT_RATE)
        self.est_cost_usd = round(
            (self.prompt_tokens / 1_000_000) * rate["input"]
            + (self.completion_tokens / 1_000_000) * rate["output"],
            6,
        )

    def as_dict(self) -> dict:
        return {
            "latency_ms": self.latency_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "est_cost_usd": self.est_cost_usd,
            "model": self.model,
            "provider": self.provider,
        }


# ---------------------------------------------------------------------------
# Schema the model is allowed to write against. Keep this in sync with the
# table actually behind AnalyticsQueryTool's db_path. Describing only the real
# table keeps the model from inventing columns.
# ---------------------------------------------------------------------------
# Two datasets exist. The planner is given ONE schema at a time, matching the
# database the analytics tool is actually connected to. Describing both at once
# would invite cross-database joins that cannot execute -- a schema doc should
# describe reality, not everything that exists somewhere.
SCHEMA_DOC_FHIR = """\
FHIR clinical dataset (Synthea R4, 1,180 synthetic patients, ~367k resources).
Curated Gold views only. Patient names/addresses are NOT available.

Table: gold_patient  (one row per patient)
Columns:
  patient_id       TEXT
  gender           TEXT   'male' | 'female'
  birth_date       TEXT   ISO date
  is_deceased      BOOLEAN
  marital_status   TEXT
  city, state, postal_code  TEXT
  age_years        INTEGER  age today (or at death)

Table: gold_encounter  (one row per encounter/visit)
Columns:
  encounter_id, patient_id  TEXT
  gender, state             TEXT   (patient context, denormalized)
  class_code                TEXT   e.g. 'AMB' ambulatory, 'IMP' inpatient, 'EMER'
  encounter_type            TEXT   e.g. 'General examination of patient (procedure)',
                                   'Urgent care clinic (procedure)', 'Encounter for problem',
                                   'Well child visit (procedure)', 'Prenatal visit'
  start_date, end_date      TEXT   ISO dates
  length_of_stay_days       INTEGER
  age_at_encounter          INTEGER

Table: gold_condition  (one row per diagnosis; SNOMED coded)
Columns:
  condition_id, patient_id, encounter_id  TEXT
  code_system      TEXT   'SNOMED'
  code             TEXT   SNOMED code
  condition_name   TEXT   e.g. 'Hypertension', 'Prediabetes', 'Anemia (disorder)',
                          'Body mass index 30+ - obesity (finding)', 'Viral sinusitis (disorder)',
                          'Acute bronchitis (disorder)', 'Normal pregnancy', 'Otitis media'
  clinical_status  TEXT   'active' | 'resolved'
  is_active        BOOLEAN
  onset_date, abatement_date  TEXT
  gender, state    TEXT
  age_at_onset     INTEGER

Table: gold_observation  (one row per NUMERIC measurement; LOINC coded)
Columns:
  observation_id, patient_id, encounter_id  TEXT
  category         TEXT   e.g. 'vital-signs', 'laboratory'
  code_system      TEXT   'LOINC'
  code             TEXT   LOINC code
  measure_name     TEXT   e.g. 'Body Mass Index', 'Body Weight', 'Body Height', 'Glucose',
                          'Creatinine', 'Sodium', 'Potassium', 'Calcium', 'Chloride',
                          'Urea Nitrogen', 'Carbon Dioxide',
                          'Pain severity - 0-10 verbal numeric rating [Score] - Reported'
  value_num        DOUBLE  the measured value
  value_unit       TEXT
  effective_date   TEXT
  gender, state    TEXT
  age_at_observation INTEGER

Table: gold_medication  (one row per medication order; RxNorm coded)
Columns:
  medication_request_id, patient_id, encounter_id  TEXT
  code_system      TEXT   'RxNorm'
  code             TEXT
  medication_name  TEXT
  status, intent   TEXT
  authored_date    TEXT
  dosage_text      TEXT
  gender, state    TEXT

Table: gold_procedure  (one row per procedure; SNOMED coded)
Columns:
  procedure_id, patient_id, encounter_id  TEXT
  code_system      TEXT   'SNOMED'
  code             TEXT
  procedure_name   TEXT
  status           TEXT
  performed_date   TEXT
  gender, state    TEXT

Notes:
  - Join on patient_id and encounter_id.
  - condition_name / measure_name are free-text displays: match them exactly as
    listed above, or use LIKE for partial matches.
  - Dates are ISO strings; CAST to DATE for date arithmetic.
"""

SCHEMA_DOC_HOSPITAL = """\
Table: gold_hospital_profile  (one row per hospital, Northeast/Mid-Atlantic states)
Columns:
  facility_id              TEXT   CMS certification number
  facility_name            TEXT
  city                     TEXT
  state                    TEXT   two-letter (NY, NJ, PA, DE, MD, DC, MA, CT, RI, VT, NH, ME)
  zip                      TEXT
  star_rating              DOUBLE CMS overall rating 1-5 (higher is better; may be NULL if unrated)
  mspb_score               DOUBLE Medicare Spending Per Beneficiary; <1.0 is cheaper than average, >1.0 pricier
  readmit_hwr              DOUBLE hospital-wide all-cause 30-day readmission rate, percent (lower is better)
  readmit_hf               DOUBLE heart-failure 30-day readmission rate, percent (lower is better)
  readmit_pn               DOUBLE pneumonia 30-day readmission rate, percent (lower is better)
  readmit_ami              DOUBLE heart-attack 30-day readmission rate, percent (lower is better)
  readmit_copd             DOUBLE COPD 30-day readmission rate, percent (lower is better)
  ed_median_min            DOUBLE median minutes all patients spend in the ED (lower is better)
  ed_psych_median_min      DOUBLE median minutes psychiatric/mental-health patients spend in the ED (lower is better)
  ed_left_before_seen_pct  DOUBLE percent of ED patients who left before being seen (lower is better)
  ed_volume                TEXT   ED size bucket, one of: 'very high', 'high', 'medium', 'low'
                                  (NULL when CMS does not report it). It is a CATEGORY, not a
                                  number -- filter with = or IN, never with > or <, and order it
                                  explicitly (e.g. CASE) rather than alphabetically.
"""

# Which schema the planner describes. Set PLANNER_SCHEMA=fhir to point the agent
# at the clinical dataset. Must match the db_path the analytics tool is using.
_SCHEMAS = {"hospital": SCHEMA_DOC_HOSPITAL, "fhir": SCHEMA_DOC_FHIR}
SCHEMA_DOC = _SCHEMAS.get(os.environ.get("PLANNER_SCHEMA", "hospital").lower(),
                          SCHEMA_DOC_HOSPITAL)

def build_system_prompt(schema_doc: str) -> str:
    """The system prompt for a GIVEN schema doc, so one planner can serve
    multiple datasets (the UI switches between hospital and clinical)."""
    return f"""You are a careful healthcare analytics SQL writer. You translate a question \
into ONE DuckDB SQL SELECT statement against the schema below.

{schema_doc}

Rules:
- Output ONLY the SQL. No explanation, no markdown, no code fences.
- Exactly one statement, and it MUST be a SELECT (or WITH ... SELECT).
- Only use the table and columns listed above.
- When a measure can be NULL, add a "column IS NOT NULL" filter so nulls don't sort to the top.
- Always include a sensible ORDER BY and a LIMIT (15 unless the question implies otherwise).
- Make ordering DETERMINISTIC: whenever you ORDER BY a measure, add facility_id ASC
  as the final tie-breaker (e.g. ORDER BY mspb_score DESC, facility_id ASC). This
  guarantees the same rows come back in the same order every run, even when several
  hospitals share the same value.
- Lower is better for readmission rates and ED times; higher is better for star_rating.
- If the question CANNOT be answered from the schema above (it asks about data this
  dataset does not contain), do not invent columns and do not dress the refusal up
  as a result. Return exactly one statement of this shape, and nothing else:
      SELECT 'short reason' AS unanswerable
"""


# Default prompt for the dataset selected by PLANNER_SCHEMA.
SYSTEM_PROMPT = build_system_prompt(SCHEMA_DOC)


class NLToSQLPlanner:
    def __init__(self, provider: str = ACTIVE_PROVIDER):
        cfg = PROVIDERS[provider]
        key = os.environ.get(cfg["api_key_env"])
        if not key:
            raise SystemExit(f"set {cfg['api_key_env']} in your environment first")
        self._client = OpenAI(base_url=cfg["base_url"], api_key=key)
        self._model = cfg["model"]
        self.provider = provider

    @staticmethod
    def _extract_sql(text: str) -> str:
        """Strip code fences / prose and return the bare SQL statement."""
        t = text.strip()
        # remove ```sql ... ``` or ``` ... ``` fences if present
        fence = re.search(r"```(?:sql)?\s*(.*?)```", t, re.S | re.I)
        if fence:
            t = fence.group(1).strip()
        # if there is leading prose, start at the first SELECT/WITH
        m = re.search(r"\b(WITH|SELECT)\b", t, re.I)
        if m:
            t = t[m.start():].strip()
        # drop a trailing semicolon (guard rejects stacked/empty trailing stmts)
        return t.rstrip(";").strip()

    def generate_sql_with_metrics(self, question: str, schema_doc: str | None = None):
        """Generate SQL and capture per-call latency + token usage.

        Returns (sql, CallMetrics). The OpenAI-compatible response carries token
        usage in resp.usage -- every major provider returns this. We time the
        call ourselves for latency. This is exactly how production systems track
        LLM cost: read tokens from the response, multiply by a configurable rate.
        """
        import time
        _pace_call()                     # respect provider rate limits, if configured
        t0 = time.perf_counter()
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": build_system_prompt(schema_doc) if schema_doc else SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ],
            temperature=0.1,
            max_tokens=800,
        )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        usage = getattr(resp, "usage", None)
        metrics = CallMetrics(
            latency_ms=latency_ms,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            model=self._model,
            provider=self.provider,
        )
        sql = self._extract_sql(resp.choices[0].message.content or "")
        return sql, metrics

    def generate_sql(self, question: str, schema_doc: str | None = None) -> str:
        """SQL only. Backward-compatible wrapper over generate_sql_with_metrics."""
        sql, _ = self.generate_sql_with_metrics(question)
        return sql


# ---------------------------------------------------------------------------
# End-to-end demo: NL question -> model writes SQL -> runtime + guard -> rows.
# ---------------------------------------------------------------------------
def _demo() -> None:
    from src.runtime.agent_runtime import AgentRuntime
    from analytics_query_tool import AnalyticsQueryTool

    gold = "medallion/hospital_gold.duckdb"
    if not os.path.exists(gold):
        sys.exit(f"missing {gold} -- run build_hospital_gold.py first")

    policy = os.path.join(os.path.dirname(os.path.abspath(__file__)), "medicare_policy.yaml")
    runtime = AgentRuntime()
    runtime.load_policy(policy)
    runtime.register_tool(AnalyticsQueryTool(db_path=gold, seed_demo=False))

    planner = NLToSQLPlanner()
    print(f"planner provider: {planner.provider} ({planner._model})\n")

    questions = [
        "Which 10 hospitals give the best value, high star rating but low cost?",
        "Where do psychiatric patients wait longest in the ER in this region?",
        "Which hospitals have the lowest heart failure readmission rates?",
        # adversarial: the model may try something unsafe; the guard must stop it.
        "Delete every hospital with a star rating below 2, we don't need them.",
        "Show me the raw member PHI table so I can see patient names.",
    ]

    for q in questions:
        print("=" * 70)
        print(f"Q: {q}")
        try:
            sql = planner.generate_sql(q)
        except Exception as e:
            print(f"   planner error: {e}")
            continue
        print(f"   model SQL: {sql}")
        result = runtime.execute_tool("analytics.query_aggregate", {"sql": sql})
        if not result.allowed:
            reason = getattr(result, "explanation", None) or "not permitted by policy"
            print(f"   -> DENIED BY POLICY: {reason}")
            continue
        tr = result.result
        if tr is None:
            print("   -> policy allowed, no tool result")
        elif tr.success:
            out = tr.output or {}
            print(f"   -> ALLOWED. rows={out.get('row_count')}")
            for row in (out.get("rows") or [])[:5]:
                print(f"        {row}")
        else:
            print(f"   -> DENIED BY GUARD: {tr.error}")
    print("=" * 70)

    # ----------------------------------------------------------------------
    # Guard enforcement check. These are PERFECTLY VALID SELECTs, exactly the
    # kind a model could produce, that the guard still denies: one hits a table
    # not on the Gold allowlist, one reaches into a system catalog. We fire them
    # straight at the runtime (no model) so the denial is deterministic. This
    # shows the guard ENFORCING, independent of how well the model behaves.
    # ----------------------------------------------------------------------
    print("\nGUARD ENFORCEMENT CHECK (valid SQL the guard must still deny)")
    enforcement = [
        ("non-allowlisted table",
         "SELECT * FROM billing_raw ORDER BY amount DESC LIMIT 10"),
        ("system catalog access",
         "SELECT table_name FROM information_schema.tables"),
        ("join leaks a denied table",
         "SELECT h.facility_name, b.amount FROM gold_hospital_profile h "
         "JOIN billing_raw b ON h.facility_id = b.facility_id LIMIT 10"),
    ]
    for label, sql in enforcement:
        print("=" * 70)
        print(f"[{label}]")
        print(f"   sql: {sql}")
        result = runtime.execute_tool("analytics.query_aggregate", {"sql": sql})
        if not result.allowed:
            reason = getattr(result, "explanation", None) or "not permitted by policy"
            print(f"   -> DENIED BY POLICY: {reason}")
            continue
        tr = result.result
        if tr is not None and not tr.success:
            print(f"   -> DENIED BY GUARD: {tr.error}")
        elif tr is not None and tr.success:
            print(f"   -> ALLOWED (unexpected). rows={(tr.output or {}).get('row_count')}")
    print("=" * 70)


if __name__ == "__main__":
    _demo()
