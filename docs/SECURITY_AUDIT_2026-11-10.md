# Security audit, 2026-11-10 (backlog item 28)

The repo against `docs/SECURITY_STANDARDS.md`. **Plan only: no code changed.** Every gap below
is a new backlog item: finding N is item 29+N, so items 30-69 (29 is the GROUP BY item in
PR #83). The owner places them in the Order of work. Security decisions are the owner's, so this PR is his to merge.

**How it was done.** Read-only, on `main` at d6f311a with #81, #82 and #84 merged
(5c44707):
- Section 14.2 first, by hand: the setting was read back from a live connection.
- The standard's other sections were split across four independent reviewers: Sections 3-4
  and 16.5 with 19.3; Sections 5-9 and 16.3; Sections 10-13; Sections 14-15. Each checked
  every MUST and SHOULD against the code, quoting line numbers.
- I re-ran the claims that decide a severity: the DuckDB settings, the UNION row cap, and
  whether the global lock still exists after #84.
- No `.env` file was read.
- Vendor settings proposed here were checked against current docs, not memory:
  - Claude Code permissions, settings and sandboxing pages (code.claude.com/docs);
  - GitHub's Dependabot options reference (`cooldown`);
  - uv's resolution docs (`exclude-newer`).

**Result: no Critical, 2 High, 21 Medium, 17 Low.** The PHI guard, the SELECT-only allowlist,
the read-only connection, the statement timeout, secret handling in git and Render, and the
audit trail of guard decisions all meet the standard. The gaps cluster in five places:
- defence in depth around the guard (14.2);
- the CI supply chain (16.5);
- identity (one shared key, self-asserted reviewer names);
- HTTP hardening (headers, size limits, public docs);
- logging of security events.

## Section 14.2 first: the agent's DuckDB connection

```
[SEVERITY: High] DuckDB external access is on and the configuration is not locked
Where: analytics_query_tool.py:155, :171, :204; data_backends.py:37
Risk: The agent's connections are read-only but report enable_external_access=true,
  lock_configuration=false, autoinstall/autoload_known_extensions=true (read back from a
  live connection, 2026-11-10). The guard's SQL deny-list is the only thing between
  generated SQL and local files (read_text('/proc/self/environ') holds the provider keys
  on Render) or the network (httpfs autoload). Twelve review rounds have each found guard
  escapes; one more would reach files and the network.
Fix: Open every agent connection with config={'enable_external_access': False,
  'autoinstall_known_extensions': False, 'autoload_known_extensions': False,
  'lock_configuration': True} (lock last), keep read_only=True, and add a test that a
  cursor cannot SET it back or read a file even with the guard bypassed.
Standard: SECURITY_STANDARDS 14.2; OWASP LLM03:2026 (excessive agency); CWE-250
```

The same connection does meet the rest of 14.2:
- it is read-only, with an allowlisted schema;
- generated SQL is parsed (single statement, SELECT only, allowlisted tables);
- every statement has a 10 s timeout;
- every generated query and guard decision is logged.

Three parts of 14.2 are not met, and each is a finding below:
- the LIMIT is not forced on set operations (finding 14);
- comments are allowed (finding 37);
- the log does not record who asked (finding 9).

## Findings

Numbered 1-40, most severe first. Finding N is backlog item 29+N (finding 1 is item 30,
finding 40 is item 69).

### High

```
[SEVERITY: High] 1. DuckDB external access on, configuration unlocked  (above)
```
```
[SEVERITY: High] 2. No GitHub Action is pinned to a full commit SHA
Where: every .github/workflows/*.yml, ~52 `uses:` lines (e.g. tests.yml:22 actions/checkout@v7,
  codeql.yml:29 github/codeql-action/init@v4)
Risk: A moved tag (the tj-actions pattern) runs attacker code in jobs that hold GEMINI_API_KEY,
  GROQ_API_KEY and, in data-refresh.yml and release.yml, a contents:write token.
Fix: Pin each to `@<40-char sha> # vX.Y.Z`; Dependabot's github-actions updates (already on)
  keep SHAs current.
Standard: 16.5, 4.4; OWASP A03:2025; CWE-829
```

### Medium

```
[SEVERITY: Medium] 3. Reviewer identity is a typed name; anyone can approve anything, including their own proposal
Where: app.py:417 (decided_by: str), app.py:932-966, approval_graph.py:275, web/index.html:1086
Risk: With the shared key, any caller approves, rejects or escalates any item under any name,
  including items escalated to someone else (`escalated_to` is never enforced). The audit
  chain records whatever was typed. approve_with_edits commits edited text with no
  groundedness check.
Fix: Reviewer from the verified key (item 22); until then mark decided_by "self-asserted";
  refuse a decision by the proposer's key or by anyone but `assigned_to`; run the
  groundedness check on edited text.
Standard: 6.1, 6.2, 15.2; OWASP A01:2025; CWE-639, CWE-862
```
```
[SEVERITY: Medium] 4. No security headers on any response
Where: app.py:289-335 (middleware), app.py:381-391 (/ui sets only Cache-Control)
Risk: No CSP, HSTS, nosniff, Referrer-Policy, Permissions-Policy or frame-ancestors. The
  Review view's Approve button can be clickjacked; with no CSP, any future XSS reads the API
  key index.html keeps in localStorage.
Fix: A header middleware: CSP `default-src 'self'; script-src 'sha256-<inline>';
  frame-ancestors 'none'; object-src 'none'; base-uri 'none'`, nosniff, Referrer-Policy
  strict-origin-when-cross-origin, Permissions-Policy, HSTS; a test on /ui.
Standard: 9, 7.3; OWASP A02:2025; CWE-1021, CWE-693
```
```
[SEVERITY: Medium] 5. No request body or field size limits
Where: app.py:396-423 (question, sql, decided_by, reason, edited_proposal: no max_length)
Risk: A 5 MB JSON body was accepted; `question` goes straight into a model prompt
  (quota, or money with the Anthropic provider) and reviewer text into the database.
Fix: Field(max_length=...) (question 2,000, sql 20,000, names 200) and a 64 KB body cap.
Standard: 7.1, 8 (API4); OWASP LLM06:2026; CWE-770
```
```
[SEVERITY: Medium] 6. Failed API-key attempts are neither rate-limited nor logged
Where: app.py:171-178 (dependency runs before the slowapi decorator); mcp_server.py:542, :549
Risk: 80 wrong keys gave 80 x 401, no 429 and no audit line: key guessing is free and
  invisible.
Fix: Count and audit 401s per client IP in middleware (or move auth after the limiter);
  same for the MCP branches.
Standard: 5.1, 12.1, 16.3; OWASP A07:2025; CWE-307, CWE-778
```
```
[SEVERITY: Medium] 7. /datasets is unauthenticated and unthrottled, and runs one query per table
Where: app.py:601-638
Risk: Any anonymous client can drive repeated count queries over every table. Since #84
  there is no global lock to starve, but it still costs CPU on a one-worker free instance.
Fix: require_api_key + RATE_LIMIT_READ, or cache the counts per data vintage as /findings does.
Standard: 8 (API4); OWASP API4:2023; CWE-770
```
```
[SEVERITY: Medium] 8. LLM consumption is limited by rate only, per process, with no budget
Where: app.py:145-168 (memory://, 20/min per key); agent_graph.py:87; nl_to_sql_planner.py:88
Risk: One /query can make up to ~12 model calls (3 attempts x plan+critic x fallback); at
  20/min that is ~240 calls/minute per key, with no daily, global, token or cost cap, and the
  limiter resets on restart. (#84 added MAX_CONCURRENT_RUNS, which bounds concurrency, not
  volume.)
Fix: A global daily request and cost budget checked against the worst case before each call
  (the scheduled_runs.py pattern), max_length on questions (5), provider-side spend caps.
Standard: 14 (LLM06:2026), 15.2; CWE-770
```
```
[SEVERITY: Medium] 9. Audit events do not say who asked or from where
Where: src/runtime/audit_logger.py:38-56; app.py:212 (agent_id="api"); render.yaml:16
Risk: No request id, principal or source IP on any event; uvicorn runs without
  --proxy-headers, so behind Render the IP would be the proxy's anyway.
Fix: request_id, principal (hash/prefix of the key) and source_ip on every AuditEvent via
  middleware; uvicorn --proxy-headers --forwarded-allow-ips.
Standard: 12.2, 14.2 (log who asked); OWASP A09:2025; CWE-778
```
```
[SEVERITY: Medium] 10. The audit trail is lost on every redeploy, is editable by the app, and has no backup
Where: app.py:154, 211-213 (JSONL under logs/); approval.py:110-145 (SQLite unless STATE_DB_URL)
Risk: Audit, traces, metrics and (without Postgres) the approval chain vanish on a Render
  restart; nothing is append-only or copied off the server; no restore has been tested.
  (README documents the loss.)
Fix: Ship audit events to an append-only store (an INSERT-only table in the item-19 Postgres,
  or a log drain); set STATE_DB_URL on the deploy; a dated restore test in docs/runbooks.
Standard: 12.2, 13; OWASP A09:2025; CWE-779
```
```
[SEVERITY: Medium] 11. Raw exception text reaches clients, including unauthenticated /health
Where: app.py:664, :690 (planner error: {e}), :906/:958 (503), :966 (400); /health at
  app.py:548-573 returns planner_error, graph_error, approval_error and the gold_db path
Risk: Provider SDK errors can carry upstream URLs, bodies or paths.
Fix: A fixed message plus trace_id to clients; details in the trace; /health reports
  exception type only (as mcp_error already does).
Standard: 11, 8 (API8); OWASP A10:2025; CWE-209
```
```
[SEVERITY: Medium] 12. The free-text question goes to the model providers unredacted
Where: nl_to_sql_planner (via app.py:651 and the graph)
Risk: 10.4 requires identifiers redacted before the model sees them. A visitor can type real
  PHI into the public demo; it goes to Gemini's free tier and on fallback to Groq. (The
  stored data is synthetic; the risk is what people type.)
Fix: An identifier pass before the planner call (item 23's redactor), log only the
  decision, and a "do not enter real patient data" notice in the UI.
Standard: 10.4, 10.3; OWASP LLM02:2026; CWE-359
```
```
[SEVERITY: Medium] 13. HTTP MCP uses one static shared key, falling back to API_KEY
Where: mcp_server.py:455-458, 539-543
Risk: No per-user scope, no audience check; one leaked key is every MCP client.
Fix: OAuth 2.1 resource-server auth (on the roadmap); until then require a distinct
  MCP_API_KEY with no fallback, and rotate it.
Standard: 15.3; CWE-798, CWE-287
```
```
[SEVERITY: Medium] 14. The row cap is not enforced on UNION / set operations
Where: sql_guard.py:293-295; analytics_query_tool.py:108 (fetchall), :323 (slice afterwards)
Risk: Verified: a top-level UNION ALL is allowed with no LIMIT, and the cap is applied by
  slicing after fetchall(). A cross join inside a UNION can exhaust 512 MB before the 10 s
  timeout.
Fix: Wrap a non-SELECT top node as SELECT * FROM (...) LIMIT cap; fetchmany(cap+1).
Standard: 14.2 (forced LIMIT); OWASP LLM06:2026; CWE-400
```
```
[SEVERITY: Medium] 15. main has no branch protection, and a workflow pushes straight to it
Where: repo settings (protection 404, rulesets []); data-refresh.yml:20-21, :117
Risk: Unreviewed code or data reaches main, and the Render deploy, without CI passing.
Fix: A ruleset on main: required checks (pytest, pip-audit, container, CodeQL, ui-e2e,
  mcp-e2e), no force push or deletion; data-refresh opens a PR instead of pushing.
  (The owner said he is turning protection on; this records it.)
Standard: 16.5, 19.3; OWASP A08:2025; CWE-284
```
```
[SEVERITY: Medium] 16. No lockfile, no hashes, no release-age cooldown for Python installs
Where: requirements*.txt (== top-level only); render.yaml:15; Dockerfile:23
Risk: Transitive dependencies resolve fresh on every build, so a compromised new release of
  one is installed.
Fix: uv with a committed uv.lock and `exclude-newer = "1 week"` (uv's documented cooldown;
  needs a recent uv), `uv sync --locked` in CI, Render and the Dockerfile. Or
  `--generate-hashes` + `--require-hashes`.
Standard: 4.3, Prime Directive 12; OWASP A03:2025; CWE-494
```
```
[SEVERITY: Medium] 17. Dependabot uses only the 3-day default cooldown
Where: .github/dependabot.yml:6-29
Risk: The standard asks for a longer release-age wait.
Fix: `cooldown: { default-days: 7 }` on pip and github-actions (documented for both;
  security updates are never delayed by it).
Standard: 4.6, 16.5; CWE-1357
```
```
[SEVERITY: Medium] 18. No secret scanning in pre-commit or CI
Where: .pre-commit-config.yaml:6-24; no gitleaks/trufflehog job
Risk: GitHub push protection covers known provider formats only; generic secrets are not
  caught. (A history scan of 405 commits found no real key, see Compliant.)
Fix: gitleaks pre-commit hook and a CI job (`gitleaks detect --redact`), pinned.
Standard: 3.3, 19.3; CWE-798
```
```
[SEVERITY: Medium] 19. Four workflows set no top-level permissions
Where: tests.yml, eval-on-push.yml, mcp-e2e.yml, eval-fhir-manual.yml
Risk: They inherit the repository's default token, which may be read/write.
Fix: `permissions: contents: read` at the top of each.
Standard: 16.5; OWASP A01:2025; CWE-250
```
```
[SEVERITY: Medium] 20. release.yml grants contents:write to the whole workflow, and credentials persist in both write workflows
Where: release.yml:25-26 (pip install … pytest unpinned, tests run under the write token);
  release.yml:33, data-refresh.yml:28 (checkout without persist-credentials: false)
Fix: contents:read at the workflow level, write only on the `gh release create` job;
  persist-credentials: false except where data-refresh pushes; pin pytest.
Standard: 16.5; OWASP A08:2025; CWE-250
```
```
[SEVERITY: Medium] 21. Base images pinned by tag, not digest
Where: Dockerfile:12, docker/Dockerfile.sandbox:1 (python:3.11-slim); docker-compose.yml:36;
  tests.yml postgres:16
Fix: Pin by @sha256 digest; Dependabot `docker` updates keep them current.
Standard: 4.4; OWASP A03:2025; CWE-494
```
```
[SEVERITY: Medium] 22. The downloaded FHIR Gold and the Synthea zip are not checksummed
Where: scripts/fetch_fhir_gold.py:93; fetch_synthea.py:56; render.yaml:15 (every deploy)
Risk: A swapped release asset loads unverified data into the deployed service.
Fix: A committed expected SHA-256 per asset; fail on mismatch.
Standard: 4.5; OWASP A08:2025; CWE-494
```
```
[SEVERITY: Medium] 23. The standalone MCP server writes no audit log
Where: mcp_server.py:131-135, 159-167 (AgentRuntime() without an audit_logger)
Risk: stdio and standalone-HTTP tool calls are not audited (only the /mcp mount is).
Fix: Attach an AuditLogger that drops rows in main() / at module start.
Standard: 15.2, 15.3, 12.1; CWE-778
```

### Low

```
[SEVERITY: Low] 24. /docs, /redoc and /openapi.json are public in production
Where: app.py:289-294; the / route advertises /docs
Fix: docs_url/redoc_url/openapi_url=None unless an env flag is set, or behind the key.
Standard: 8 (API9), 16.3
```
```
[SEVERITY: Low] 25. Auth is per route instead of deny-by-default; /metrics and /schema are public
Where: app.py:595, :849
Fix: Require auth on the router and list public routes explicitly (/health, /ui, /ui-config).
Standard: 6.2, 16.3; CWE-862
```
```
[SEVERITY: Low] 26. Request models accept unknown fields; most routes have no response_model
Where: app.py:396-423
Fix: model_config = ConfigDict(extra="forbid"); response models.
Standard: 6.2, 16.3; OWASP API3:2023; CWE-915
```
```
[SEVERITY: Low] 27. One shared plaintext API key, no scopes or revocation
Where: app.py:115, :174; mcp_server.py:455-457
Fix: Backlog item 22 (hashed per-user keys, roles, revocation). Recorded, not a new item.
Standard: 5.3; OWASP API2:2023
```
```
[SEVERITY: Low] 28. /ui-config prefills the API key for any loopback-looking client
Where: app.py:567-585
Risk: Safe on Render; leaks the key behind a same-host proxy that keeps 127.0.0.1.
Fix: Prefill only with an explicit UI_PREFILL_KEY=1.
Standard: 3.2, 5.3; CWE-200
```
```
[SEVERITY: Low] 29. esc() does not escape the single quote
Where: web/index.html:428
Fix: Add ' -> &#39;. Not exploitable today (all dynamic attributes are double-quoted).
Standard: 7.3; CWE-79
```
```
[SEVERITY: Low] 30. No error tracking or security alerts
Where: repository-wide (eval, uptime and SLO alerts exist)
Fix: Alert on auth-failure and spend thresholds through a GitHub issue (nightly_alerts
  pattern); optional free Sentry with send_default_pii=False and a scrubber (new dependency:
  owner's OK).
Standard: 12.3; CWE-778
```
```
[SEVERITY: Low] 31. No retention limit on logs holding free text
Where: cost_tracker.py:30-31 (question[:200], forever); audit JSONL (never rotated);
  approval.py (questions and proposals kept forever)
Fix: Store a hash and length in the metrics log; rotate and cap JSONL; a retention period and
  purge for approvals.
Standard: 10.1; CWE-532
```
```
[SEVERITY: Low] 32. The Postgres connection does not require TLS
Where: state_db.py:80
Fix: sslmode=require (or verify-full) unless the URL sets one.
Standard: 10.2; CWE-319
```
```
[SEVERITY: Low] 33. Secret redaction misses this repo's own key formats
Where: src/utils/redaction.py:36-37 (sk-, ghp_, Bearer only)
Fix: Patterns for AIza (Gemini), gsk_ (Groq), xai- (xAI), sk-ant- (Anthropic), with tests.
Standard: 10.3; CWE-532
```
```
[SEVERITY: Low] 34. Audit failures are swallowed silently
Where: app.py:727-728 (except: pass), app.py:214-215
Fix: Warn on stderr with the exception type; /health shows audit_enabled.
Standard: 11; CWE-390
```
```
[SEVERITY: Low] 35. Hosted trace export masks single-quoted literals only
Where: trace_export.py:134-139; analytics_query_tool.py:53-56
Fix: Strip SQL comments before masking; hash or truncate the critic's reason for hosted
  export. (Export is off by default.)
Standard: 10.3; CWE-359
```
```
[SEVERITY: Low] 36. Old audit logs are committed although logs/ is gitignored
Where: logs/audit/audit_agent_*.jsonl (10 files, demo prompts only)
Fix: git rm --cached logs/audit/*.jsonl.
Standard: 10.4; CWE-532
```
```
[SEVERITY: Low] 37. SQL comments are allowed and kept in the executed SQL
Where: sql_guard.py:186-198 (verified: /* */ and -- pass into safe_sql, which the critic reads)
Risk: The model can plant text aimed at the critic. The critic can only make the agent
  stricter, so impact is low.
Fix: Deny statements with comments, or emit with comments=False.
Standard: 14.2 (no comments); OWASP LLM01:2026; CWE-89
```
```
[SEVERITY: Low] 38. Retry and critic prompts do not delimit untrusted text
Where: agent_graph.py:205-211; nl_to_sql_planner.py:439-466, :613
Fix: Labelled delimiters around question, error and SQL; an "untrusted text" rule in the
  planner's system prompt (as the critic already has). Changes prompts: re-record replays.
Standard: 14 (LLM01:2026)
```
```
[SEVERITY: Low] 39. Adversarial evals are not a CI gate; no file/network-exfiltration case
Where: eval_bank_adversarial.py (13 cases), no workflow runs it
Fix: A replayed adversarial run in eval-on-push (push_gate pattern); add read_text, ATTACH
  and URL cases.
Standard: 14.2, 15.2; OWASP LLM01:2026
```
```
[SEVERITY: Low] 40. Smaller supply-chain and setup gaps
Where: .gitignore (only `.env`; stray BOM on line 1); tests.yml:78 (pip-audit skips
  requirements-e2e.txt; dev extras in setup.py are >=); container.yml:46-53 (Trivy 0.58.2
  with the Docker socket mounted); repo settings (private vulnerability reporting off,
  Dependabot security updates off); no workflow linter; mcp_server.py:423-447 (demo tools
  echo/fetch_url/git_push advertised on the production /mcp, all denied by policy); no
  single kill switch for the agent endpoints.
Fix: .gitignore patterns (.env.*, !.env.example, *.pem, *.key, *.p12, *.tfstate, secrets/);
  audit every requirements file and pin dev extras; current Trivy on a `docker save`
  tarball (--input) instead of the socket; turn on private vulnerability reporting and a
  SECURITY.md pointing to it; Dependabot security updates; zizmor in CI; demo tools behind
  MCP_DEMO_TOOLS=1; an AGENT_DISABLED=1 flag returning 503 on /query, /propose and /mcp.
Standard: 3.1, 4.3, 4.4, 19.3, 15.2, 15.3
```

## Compliant (MUST rules already met)

- **Secrets.** Git history (405 commits) holds no real key-shaped string and no private key;
  the two hits are a synthetic test fixture and a `user:password` placeholder.
  - `.env` is gitignored and was never committed; `.env.example` has names only.
  - Every secret in render.yaml is `sync: false`; `.dockerignore` excludes `.env*`.
  - The API key travels only in headers, compared in constant time; the service refuses to
    start without it (and MCP HTTP too) unless ALLOW_OPEN_ACCESS.
- **SQL.** Every path goes through the guard: parsed, single statement, SELECT only,
  allowlisted tables, catalog schemas and file-reader functions denied, and a read-only
  connection.
  - The app's own SQL is parameterized.
  - Statement timeout 10 s.
  - The PHI guard and `check_output` hold FHIR results to aggregates of 11+.
- **Output.** Every model, SQL and data value in the UI goes through `esc()` before
  `innerHTML`.
  - No third-party scripts, eval, remote images or open redirects; no CORS middleware, and
    no cookies, so no CSRF.
  - DB rows never reach a prompt (the critic sees question and SQL only), and error text fed
    back to the model withholds data values.
- **Logging.** Guard and policy denials, tool calls, critic reviews and answer outcomes are
  audited as structured JSONL.
  - Result rows are never logged, traced, cached or checkpointed.
  - A hosted trace backend gets the question only as a hash.
- **Agents.** Bounded attempts (3), and human approval before any consequential action.
  - No external-communication leg on /query or MCP, so the lethal trifecta is not complete:
    `http.fetch` needs an approval with no callback, and `git.push` is not allowed.
  - MCP checks the key, the Origin and a per-key limit, and binds to 127.0.0.1 by default.
  - No third-party MCP servers are configured in the repo.
- **CI.** pip-audit, CodeQL, Trivy (by digest), Dependabot, and GitHub secret scanning with
  push protection are on.
  - No `pull_request_target`, no untrusted `github.event` text in `run:`, and secrets only on
    push, schedule and dispatch.
  - The Dockerfile runs as a non-root user (checked in CI).

## Proposed `.claude/settings.json` (Section 17.2, tailored)

Not added in this PR. This is the proposal; the owner commits it. Checked against the
Claude Code docs (code.claude.com/docs/en/permissions, /settings, /sandboxing, read
2026-11-10):
- deny is evaluated first, then ask, then allow;
- `Bash(x *)` also matches bare `x`;
- compound commands are matched per subcommand;
- `Read(./path)` is relative to the working directory;
- `~/` is supported;
- `WebFetch` alone matches every fetch;
- `disableBypassPermissionsMode` is valid in project settings.

```json
{
  "permissions": {
    "defaultMode": "default",
    "disableBypassPermissionsMode": "disable",
    "deny": [
      "Read(./.env)", "Read(./.env.*)", "Read(./**/.env)", "Read(./**/.env.*)",
      "Read(./secrets/**)", "Read(./**/*.pem)", "Read(./**/*.key)", "Read(./**/*.tfstate)",
      "Read(~/.ssh/**)", "Read(~/.aws/**)", "Read(~/.config/gcloud/**)", "Read(~/.kube/**)",
      "Read(~/.npmrc)", "Read(~/.pypirc)",
      "Bash(git push --force *)", "Bash(git push -f *)", "Bash(git push --force-with-lease *)",
      "Bash(git push origin main)", "Bash(git push origin HEAD:main)",
      "Bash(rm -rf *)", "Bash(sudo *)",
      "Bash(gh workflow run release.yml *)", "Bash(git tag *)",
      "Bash(render *)"
    ],
    "ask": [
      "Bash(npm install *)", "Bash(npm i *)", "Bash(pnpm add *)",
      "Bash(pip install *)", "Bash(uv add *)", "Bash(uv pip install *)",
      "Bash(npx *)", "Bash(wget *)",
      "Bash(terraform apply *)", "Bash(terraform destroy *)",
      "Bash(kubectl delete *)", "Bash(kubectl apply *)", "Bash(helm install *)", "Bash(helm upgrade *)",
      "Bash(supabase db push *)", "Bash(vercel *)",
      "Bash(gh pr merge *)", "Bash(gh release *)",
      "Bash(databricks *)"
    ],
    "allow": [
      "Bash(git status)", "Bash(git diff *)", "Bash(git log *)", "Bash(git show *)",
      "Bash(git fetch *)", "Bash(git checkout -b *)", "Bash(git add *)", "Bash(git commit *)",
      "Bash(git push -u origin claude/*)", "Bash(git push origin claude/*)",
      "Bash(pytest *)", "Bash(python -m pytest *)", "Bash(ruff check *)", "Bash(ruff format *)",
      "Bash(mypy *)", "Bash(python sql_guard.py)", "Bash(python eval_harness.py *)",
      "Bash(python scripts/record_push_gate.py *)", "Bash(python scripts/load_test.py *)",
      "Bash(gh pr view *)", "Bash(gh pr checks *)", "Bash(gh run view *)",
      "Bash(gh workflow run eval-fhir-manual.yml *)",
      "WebFetch(domain:code.claude.com)", "WebFetch(domain:docs.github.com)",
      "WebFetch(domain:docs.astral.sh)", "WebFetch(domain:duckdb.org)"
    ]
  }
}
```

**Each change from the standard's template, and why.** "Unattended runs don't stall" means
routine work in this repo must not stop at an ask prompt, while anything irreversible still
does.
- **Deny list:** kept as is, with these additions:
  - **Force pushes:** `--force-with-lease` is added, because it is still a force push.
  - **Direct pushes to main** are denied: work goes through a branch and PR (CLAUDE.md), and
    branch protection will refuse them anyway.
  - **`git tag` and dispatching `release.yml`:** a tag publishes a release and an image,
    which is the owner's step (directive 13).
  - **`render`** (the Render CLI) changes the live deploy.
  - These are additions only: nothing the template denies is allowed.
- **Ask list:**
  - **Removed `Bash(git push *)`.** Every unattended task pushes its branch, so it would stall
    every run. It is replaced by allow rules for the `claude/*` branches only. A push to any
    other branch still matches no allow rule, so it falls to the default prompt, and pushes to
    main and force pushes are denied above.
  - **Removed `Bash(curl *)` and `WebFetch`.** The CI-status and docs lookups the work needs
    are covered by narrow allow rules: `gh` reads and four documentation domains. Any other
    fetch is not in the allow list, so it still prompts under `defaultMode: default`.
    Arbitrary `curl` still prompts for the same reason.
  - **Removed `Bash(docker run *)`.** The container smoke test runs in CI (container.yml), not
    locally, so it is no longer needed. It still prompts by default because it isn't allowed.
  - **Added `gh pr merge` and `gh release`:** merges happen under the owner's standing OK, so
    each one asks; releases are the owner's.
  - **Added `databricks *`:** a real workspace, so directive 13 applies.
- **Allow list:** the template's Python entries, plus this repo's checks:
  - pytest, ruff, mypy;
  - the guard harness;
  - the eval harness, which runs on the free-tier key from the environment and never reads
    `.env`;
  - the push-gate recorder and the load test (both replay only, $0);
  - read-only `gh`;
  - dispatching the manual FHIR eval, which only reads.
- **Not proposed:** `defaultMode: dontAsk` or `bypassPermissions`. The docs say bypass does not
  take effect from project settings, and the standard forbids it outside an isolated container.
  Unattended cloud runs already use their own session mode.
- **Sandbox:** the docs support `sandbox.enabled` on Linux. It is set in user or managed
  settings, not in the repo, so it is a step for the owner's machines.
- **Caveat from the docs:** Read deny rules also cover `cat`, `head`, `tail`, `sed` and `tee`
  in Bash. They do not cover `grep -r` or a Python script that opens the file. Only the
  sandbox enforces file access, so the deny list is a guardrail, not a boundary (17.1).

## Proposed placement (the owner decides)

Nothing is Critical, so nothing jumps the queue by the item's rule. Proposed order:
1. **Highs first:** items 30 (DuckDB external access, one small PR) and 31 (pin Actions to
   SHAs).
2. **The Mediums that close real exposure, before item 22:**
   - API: 33 (security headers), 34 (size limits), 35 (401 throttling and logging),
     36 (/datasets), 37 (consumption budget), 40 (exception text);
   - the guard: 43 (UNION row cap);
   - CI and supply chain: 44-51 (branch protection, lockfile, cooldown, secret scanning,
     workflow permissions, release permissions, image digests, checksums).
3. **Item 22**, which closes or unblocks 32 (reviewer identity), 38 (who asked), 42 (MCP key)
   and 56 (shared key).
4. **Then the rest:**
   - 39 (append-only audit trail; needs the item-19 Postgres on the deploy);
   - 41 (question redaction; with item 23);
   - 52 (standalone MCP audit);
   - the Lows 53-69, in any order.
