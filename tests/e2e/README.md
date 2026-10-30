# End-to-end tests

Real processes, real sockets, a real browser. Collected only with `RUN_E2E=1`
(`conftest.py`), so the default `pytest` run never starts a server.

| file | what | workflow |
|---|---|---|
| `test_mcp_e2e.py` | an MCP client over stdio, standalone HTTP and `/mcp` | `mcp-e2e.yml`, every push |
| `test_ui_e2e.py` | the API and the UI in Chromium, on recorded LLM responses | `e2e.yml`, every push |

```bash
pip install -r requirements-api.txt -r requirements.txt -r requirements-e2e.txt
python -m playwright install chromium
RUN_E2E=1 pytest tests/e2e -q
```

## Recorded responses (`recordings/llm.json`)

`test_ui_e2e.py` starts the API with `PLANNER_PROVIDER=replay` (`replay.py`).
Every plan and critic call is looked up by
`sha256(role, sha256(system prompt), user text)`. The system prompt carries the
schema, and the user text carries the question and any retry context.

- **No network.** CI holds no LLM key, and the replay provider never calls one.
- **A miss fails loudly** with `ReplayMiss`. That happens when a question, a
  prompt or the schema changes. It is meant to: a stale recording must not pass.
- **Recordings hold response text only.** That is SQL or a critic verdict, plus
  token counts and the model name. They hold no keys and no result rows: the
  model never sees rows. `tests/unit/test_replay.py` checks for both.

### Re-recording

This is a manual, local, one-off step after a prompt, schema or scenario change.
Misses are answered by a real provider and written to the store. Existing
entries are kept, so delete `recordings/llm.json` first for a clean set:

```bash
RUN_E2E=1 E2E_RECORD=1 REPLAY_SOURCE_PROVIDER=groq PLANNER_MIN_INTERVAL_SEC=5 \
    pytest tests/e2e/test_ui_e2e.py -q          # needs GROQ_API_KEY (or gemini, ...)
RUN_E2E=1 pytest tests/e2e/test_ui_e2e.py -q    # then replay it, offline
git add tests/e2e/recordings/llm.json
```

**The committed set is authored, not recorded.** Each entry is marked
`"authored": true` and was written by `author_recordings.py`. That script runs
the real graphs (the API's runtime, guard, policy and schema mapping, plus the
approval graph) with a scripted source, so the keys are exactly the requests
the server makes.

There are two reasons. First, the guard-denial scenario needs SQL that reads
`bronze_patient` on every attempt, which a well-behaved model declines to write.
Second, these scenarios test the system (UI, API, graph, guard, approvals), not
a model's SQL, so a reviewed, correct query per question is the steadier
fixture.

Re-run the script after a prompt or scenario change:

```bash
rm tests/e2e/recordings/llm.json && python tests/e2e/author_recordings.py
```

Or record from a real model instead (above). That re-records every entry except
the denial scenario, which only the script produces.
