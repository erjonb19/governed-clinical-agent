# Owner steps

Steps only the owner can do by hand: laptop commands, Render, GitHub or Console
settings. One section per step: what to do, why it matters, and the exact commands
for **Windows PowerShell**. The agent adds a step here whenever work needs one, and
ticks it when the owner says it is done (standing rule, CLAUDE.md). No secret ever
goes in this file: where a command needs one, it reads it from your environment or
a prompt.

**Once, before the GitHub steps:** install and sign in to the GitHub CLI.

```powershell
winget install --id GitHub.cli -e
gh auth login
```

---

## [ ] 1. Dismiss code scanning alert #10 as a false positive

**Why:** CodeQL flags `nl_to_sql_planner.py:563` as clear-text logging of a
secret. The line prints the environment variable's **name** (for example
`GROQ_API_KEY`) to say the fallback is off; the value is only read with
`os.environ.get()` to test whether it is set, and never printed. The agent's
GitHub tools cannot dismiss alerts.

```powershell
gh api -X PATCH repos/erjonb19/governed-clinical-agent/code-scanning/alerts/10 `
  -f state=dismissed `
  -f dismissed_reason="false positive" `
  -f dismissed_comment="Prints the env var NAME (e.g. GROQ_API_KEY), never its value; the value is only read via os.environ.get() to test presence."
```

Check (should print `dismissed`):

```powershell
gh api repos/erjonb19/governed-clinical-agent/code-scanning/alerts/10 --jq .state
```

---

## [x] 2. Let GitHub Actions open pull requests (backlog item 44)

**Done (owner, 2026-11-14).**

**Why:** the monthly data refresh now opens a PR instead of pushing to `main`.
Without this setting its `gh pr create` is refused and the job fails (loudly, by
design). The token stays read-only by default; the setting lets Actions create and approve PRs (GitHub has one switch for both), and `main`'s ruleset still decides what can merge.

```powershell
gh api -X PUT repos/erjonb19/governed-clinical-agent/actions/permissions/workflow `
  -f default_workflow_permissions=read `
  -F can_approve_pull_request_reviews=true
```

Check (should print `true`):

```powershell
gh api repos/erjonb19/governed-clinical-agent/actions/permissions/workflow --jq .can_approve_pull_request_reviews
```

---

## [x] 3. Confirm the ruleset on `main` (backlog item 44)

**Done (owner, 2026-11-14):** branch protection on `main` requires the 8 checks plus `secret-scan`, with bypass off.

**Why:** item 44 is done only when `main` requires the checks to pass and blocks
force pushes and deletion. PRs already show "blocked" until checks pass, so a rule
looks active, but the agent cannot read rulesets to confirm what it contains.

List what applies to `main`:

```powershell
gh api repos/erjonb19/governed-clinical-agent/rules/branches/main --jq ".[].type"
```

It should include `required_status_checks`, `non_fast_forward` (no force push)
and `deletion`. To see the required check names (works in PowerShell 5.1 and 7):

```powershell
(gh api repos/erjonb19/governed-clinical-agent/rules/branches/main | ConvertFrom-Json) | Where-Object type -eq 'required_status_checks' | ForEach-Object { $_.parameters.required_status_checks.context }
```

They should include `pytest`, `pip-audit`, `image` (container), `analyze (python)`
and `analyze (actions)` (CodeQL), `ui-e2e` and `mcp-e2e`. If anything is missing, add it under
Settings -> Rules -> Rulesets -> (the ruleset for `main`), then tell the agent
"step 3 done".

---

## [ ] 4. Confirm Render built the hash-locked install (backlog item 45)

**Why:** `render.yaml` now builds with `pip install --require-hashes -r
requirements-api.lock`. Render applies a `render.yaml` change only if the service
follows the Blueprint; if the build command was set in the dashboard, Render keeps
the old unhashed install. A failed build also leaves the old deploy serving, so
the live check passing does not prove the new build is live.

1. Open the Render dashboard -> `governed-clinical-agent` -> **Events**.
2. Open the newest deploy. It should be for commit `ee16e16` or later.
3. In its build log, look for: `pip install --require-hashes -r requirements-api.lock`.
4. If the log shows `pip install -r requirements-api.txt` instead: Settings ->
   Build Command -> set it to
   `pip install --require-hashes -r requirements-api.lock && python scripts/fetch_fhir_gold.py`,
   save, and redeploy.

Check the service answers afterwards:

```powershell
Invoke-RestMethod https://governed-clinical-agent.onrender.com/v1/health
```
