# Runbook: back up, restore and verify the audit trail (backlog item 39)

The audit trail lives in two places:

- `logs/audit_api_*.jsonl`: on Render's disk, so it is lost on every redeploy.
- the `audit_events` table in the item-19 Postgres (Neon), when `STATE_DB_URL` is set. This is the copy that lasts.

The table is **append-only**: a trigger refuses every `UPDATE`, `DELETE` and `TRUNCATE`.

It is also **hash-chained**. Each row stores `hash = sha256(prev_hash + its content)`. So if someone with DDL rights switches the trigger off and edits, removes or reorders rows, `verify` reports the first row that no longer fits.

## Check the chain

Run this from the repo folder, with `STATE_DB_URL` set to the database:

```powershell
python audit_store.py verify
```

It prints `{"ok": true, "rows": N, "first_bad_seq": null}` and exits 0. If the chain is broken, it exits 1 and names the first bad `seq`.

## Back up

**Neon keeps 6 hours of history on the Free plan.** To roll the whole database back within that window, use the Neon console: **Branches -> Restore**. For anything older you need your own copy.

To take your own copy, the laptop needs the PostgreSQL client tools (`pg_dump`, `pg_restore`). Installing them is the owner's choice: the official installer from postgresql.org has a "Command Line Tools" option.

```powershell
$env:PGURL = Read-Host "Neon connection string"      # not saved anywhere
pg_dump --format=custom --no-owner --dbname=$env:PGURL --file="gca-state-$(Get-Date -Format yyyy-MM-dd).dump"
Remove-Item Env:PGURL
```

Keep the dump off the repo and off any public place. It holds approval reasons, key ids and client addresses.

## Restore

1. Create an empty database, either a new Neon branch or a new database in the project.
2. Restore the dump into it:

   ```powershell
   pg_restore --no-owner --dbname="<new database's connection string>" gca-state-YYYY-MM-DD.dump
   ```

3. Point `STATE_DB_URL` at the new database and run `python audit_store.py verify`. It must say `"ok": true` with the expected row count.
4. Put the new connection string in Render's `STATE_DB_URL` (owner step 14, part 4).

The restore brings back the triggers with the table. The restored copy is append-only at once.

## Restore test log

| Date (UTC) | Where | Rows | Result |
|---|---|---|---|
| 2026-11-24 | Local Postgres 16.15 (this repo's test container), not Neon | 50, written in two batches | `pg_dump --format=custom` (7,210 bytes), then `pg_restore --no-owner` into an empty database. `verify`: `{"ok": true, "rows": 50, "first_bad_seq": null}`. A `DELETE` on the restored table was refused by `audit_events_append_only()`. |

A restore test against the real Neon database waits for owner step 14. Add a row here when it is done.

## What this does not protect against

- **Someone with the database owner's password.** They can drop the table or the triggers. They can also rewrite every row *and* recompute the whole chain. The chain only catches edits made by someone who did not recompute it. A dated off-site dump is what catches a full rewrite: compare its rows with the live table.
- **Events the writer never wrote.** If the background writer can't reach the database, it counts the failure (`/metrics`, item 63) and the JSONL still has the event. The table does not.
