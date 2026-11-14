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

## [x] 3. Confirm the branch protection on `main` (backlog item 44)

**Done (owner, 2026-11-14):** classic branch protection on `main` (not a ruleset)
requires 9 checks with bypass off, no force push and no deletion. `pip-audit` is
not among them: see step 5.

**Why:** item 44 is done only when `main` requires the checks to pass and blocks
force pushes and deletion.

Check what is required (works in PowerShell 5.1 and 7):

```powershell
(gh api repos/erjonb19/governed-clinical-agent/branches/main/protection | ConvertFrom-Json).required_status_checks.contexts
```

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

---

## [ ] 5. Add `pip-audit` to the required checks on `main` (backlog item 44)

**Why:** item 44's "Done when" lists `pip-audit` (the dependency vulnerability scan)
among the required checks, and it is not required today, so a PR that adds a
dependency with a known vulnerability could still merge. If you would rather leave
it out, tell the agent, and it records that decision instead.

1. GitHub -> the repo -> Settings -> Branches -> the rule for `main` -> Edit.
2. Under "Require status checks to pass", search for `pip-audit` and add it.
3. Save changes.

Check (the list should now include `pip-audit`):

```powershell
(gh api repos/erjonb19/governed-clinical-agent/branches/main/protection | ConvertFrom-Json).required_status_checks.contexts
```

---

## [ ] 6. Turn on GitHub Pages for the wake-up page (backlog item 9)

**Why:** on Render's free plan the service sleeps when idle, and a sleeping
service cannot show its own "please wait" note. The wake-up page (`site/`,
published by `.github/workflows/pages.yml`) shows that note, pings the service,
and opens it once it answers. Pages is free on a public repo; the page holds no
key and no secret. The workflow cannot publish until Pages is set to deploy
from GitHub Actions.

```powershell
gh api -X POST repos/erjonb19/governed-clinical-agent/pages -f build_type=workflow
```

(If it answers that Pages already exists, run this instead:
`gh api -X PUT repos/erjonb19/governed-clinical-agent/pages -f build_type=workflow`.)

Then publish it once:

```powershell
gh workflow run pages.yml --repo erjonb19/governed-clinical-agent --ref main
```

Check (should print the page address,
`https://erjonb19.github.io/governed-clinical-agent/`):

```powershell
gh api repos/erjonb19/governed-clinical-agent/pages --jq .html_url
```

Open that address in a browser: it should say "Waking the service up" and then
open the service. Tell the agent "step 6 done", and it will point the README's
links at the page.
