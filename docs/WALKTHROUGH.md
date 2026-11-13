# Three-minute walkthrough (script to record)

For the owner to record. Four beats: ask, a guard refusal, an approval, a
trace. It uses today's UI (`/ui`); it will be updated when item 9 reworks the
first screen. Times are targets.

**Before recording:** run locally so there is no cold start and the key is
prefilled (`uvicorn app:app`, open `http://localhost:8000/ui`), or open the
deployed site a minute early to wake it, with your key entered. Do not show
the key on screen.

## 0:00-0:20 -- What this is

Show: the Overview.

Say: "This is a governed AI analyst over public Medicare hospital data and
synthetic patient records. You ask in plain English; a model writes the SQL; a
security layer decides whether that SQL may run. These four findings at the top
are fixed, reviewed queries, so no model writes those numbers."

## 0:20-1:00 -- Ask a question

Do: open **Ask**, type *Which hospitals deliver the best value -- high quality
at lower cost?*, press **Get answer**.

Say: "The model plans a query, a second model call reviews it, and the guard
checks it before it touches the data. The answer comes back with the SQL it
ran, the model, the time it took and its estimated cost."

Point at: the answer table, the model / time / cost line, and open **How this
answer was produced** to show the SQL.

## 1:00-1:40 -- The guard says no

Do: switch on **Advanced**, switch the mode from **Question** to **SQL**, run
`SELECT * FROM information_schema.tables`.

Say: "Whatever writes the SQL, a model, a person or an MCP client, it goes
through the same guard. This one reads the database catalog, which is never
allowed, so it is refused, and the refusal names the rule. On patient data
the guard also refuses anything row-level or about fewer than eleven people."

Point at: the denial and the rule it names.

## 1:40-2:20 -- A human approves

Do: open **Review**, enter a reviewer name, type *Which hospitals have the worst
heart failure readmission rates?*, press **Draft & pause**, then **Approve** it.

Say: "Some actions wait for a person. The agent pauses at a checkpoint; the
reviewer can approve, reject, edit, or escalate to someone else, and the agent
stays paused until the new owner decides. Every decision is recorded with who
made it."

Point at: the queue item, the decision buttons, the recorded reviewer.

## 2:20-3:00 -- Every step is traced

Do: open **Traces**, open the run from the Ask step.

Say: "Each run is a trace: plan, critic, guard, execute, every routing
decision, with timings. It keeps the question and the SQL, never the result
rows. CI replays recorded answers on every push, runs the full evals nightly,
and opens an issue when a score drops."

End on: the span waterfall.

## If something goes wrong while recording

- A slow first answer on the deployed site is Render's free-plan cold start;
  wait and ask again.
- A "provider unavailable" message is the free model quota; record locally on
  replayed answers instead (`PLANNER_PROVIDER=replay`, `ALLOW_REPLAY=1`, see
  `tests/e2e/README.md`). Only recorded questions answer then: in the Ask beat
  use *How many hospitals have an overall star rating of exactly 5?*.
