# UI plan (backlog item 9) -- for the owner's OK before any code

Status: **plan only.** Nothing here is built. Building starts after the owner
approves it, including the three decisions in section 3.

## 1. Goal (from the backlog)

A non-technical first-time visitor reaches a useful answer in **one or two
clicks**, in plain language, with a short **definitions** section on the first
screen and a **note that the first load can take a moment** (Render free plan
cold start). An E2E test checks the two-click claim.

## 2. What is there today (read from `web/index.html`, `app.py`, `findings.py`)

- The page lands on **Overview**: four fixed, reviewed findings (`findings.py`:
  heart-failure readmissions by state, quality and cost together, behavioral
  health ED wait, leaving the ED unseen). No LLM writes them. A definitions list
  (`#ovDefs`) and a "how this works, and its limits" card already sit below.
- **Eight tabs** in the top bar: Overview, Ask, Review, Data, Guide, Trust,
  Usage, Traces, plus an Advanced switch and a Connect button.
- **The blocker:** `/findings` requires the API key
  (`dependencies=[Depends(require_api_key)]`). On the deployed site a visitor
  with no key sees "This service is protected. Enter your access key" and **no
  answer at all**. Locally the server prefills the key, which is why the
  Overview looks fine in the E2E test (`test_7`).
- **Cold start:** Render free spins the service down when idle. The page itself
  is served by the sleeping service, so **nothing on the page can show before
  the service wakes** -- the browser just waits. A note on the page can only
  cover the slow first data load after the HTML arrives.

What "clunky" meant to leadership is not written down; this plan assumes it
means the key wall, the eight tabs and the technical wording. Please correct me
if it was something else.

## 3. Decisions needed from the owner

**D1. Let a visitor see the fixed findings without a key?**
- **(a) Recommended:** a new read-only `GET /public/findings` with no key,
  serving only the cached hospital findings (public CMS data, fixed reviewed
  SQL, the same guard path, no LLM, $0). Its own tight rate limit (for example
  `10/minute` per IP). FHIR (synthetic patient data) never goes through it,
  and `/query`, `/raw-sql`, `/propose`, `/mcp` keep the key. This is an
  **auth change**, so it needs your explicit yes; it would get a security
  review and tests that every other route still returns 401 without a key.
- (b) Keep the key wall. Then "two clicks" holds only for people you give a
  key to, and the first screen for anyone else stays "enter your key".

**D2. How to show a cold-start note**
- **(a) Recommended:** a static landing page on **GitHub Pages** ($0, public
  repo) that shows "Waking the service up -- this can take about a minute on
  the free plan", pings `/health` (a `no-cors` fetch; no CORS change needed)
  and sends the visitor to `/ui` once it answers. The README and demo links
  point at the Pages URL. New hosting surface: one static HTML file, no
  secrets, no build step.
- (b) In-page only: a banner while `/findings` is pending, plus a line in the
  README next to the link. Simpler, but it never shows during the actual wake.

**D3. Navigation**
- **(a) Recommended:** three tabs for everyone -- **Answers** (today's
  Overview, reworked), **Ask** (free-text, needs a key), **About** (Guide +
  Trust + definitions merged). Review, Data, Usage and Traces move behind
  **Advanced** (still one click away, and the Review queue keeps its badge).
- (b) Keep all eight tabs, change only the first screen.

## 4. The first screen (with D1a, D2a, D3a)

Top to bottom:
1. One sentence: what this is ("Medicare's public hospital quality data for 12
   Northeast and Mid-Atlantic states, answered in plain English") and the data
   date.
2. **"Pick a question"**: 4 to 6 large buttons worded as questions, each mapped
   to one fixed finding -- for example "Which states have the highest
   heart-failure readmissions?", "Which hospitals are high quality and low
   cost?", "Where do mental-health patients wait longest in the ED?", "Where do
   patients leave the ED before being seen?". **One click** shows the answer: a
   plain sentence, the key number, a small table, and "How we know" (the exact
   query) folded away. New questions are added to `findings.py` only, reviewed
   like the existing four (no LLM, no new data).
3. **"What the words mean"**: today's definitions, shortened to the terms the
   questions use, visible without scrolling on a laptop.
4. A short "limits" line (measures, not verdicts; lags by months) linking to
   About.
5. "Ask your own question" -> the Ask tab (key holders).

The answer to a question replaces nothing else on the screen, so the visitor
can click the next question straight away.

## 5. How it is checked

- New E2E test (`tests/e2e/test_ui_e2e.py`): open `/ui` **with no key in
  storage and no prefilled key** (the deployed case), click one question
  button, and assert a plain-sentence answer with a number appears -- **one
  click**, under the two-click bar. Also assert the definitions section is on
  the first screen and the cold-start note element exists.
- Security tests: every route other than `/public/findings`, `/health`,
  `/ui` and `/ui-config` still returns 401 without a key; `/public/findings`
  serves only the hospital set and never FHIR.
- `live-check.yml` (no key, fails on any console error) must stay green: the
  first visit must make no 401 call.
- The existing E2E tests 1-7 are updated for the new tab names, not removed.

## 6. Out of scope

New data, new measures, an LLM on the first screen, charts beyond what the
findings already return, OAuth or accounts, and any paid hosting.

## 7. Order of work once approved (one PR each)

1. `/public/findings` + security tests (only with D1a).
2. First screen and navigation (`web/index.html`) + the new E2E test.
3. GitHub Pages wake-up page + README links (only with D2a).
4. Tick item 9 in BACKLOG, PROGRESS line, README checklist.

Cost: $0 throughout. No new dependency.
