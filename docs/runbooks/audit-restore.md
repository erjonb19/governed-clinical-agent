# Runbook: back up, restore and verify the audit trail (backlog item 39)

The audit trail lives in two places:

- `logs/audit_api_*.jsonl`, on Render's disk. It is lost on every redeploy.
- The `audit_events` table in the item-19 Postgres (Neon), when `STATE_DB_URL` is set. This is the copy that lasts.

The table is **append-only**: triggers refuse every `UPDATE`, `DELETE` and `TRUNCATE`, unless someone with the table owner's rights switches them off first.

It is also **hash-chained**. Each row stores `hash = sha256(prev_hash + its content)`. So if someone edits, removes or reorders rows, `verify` reports the first row that no longer fits. Removing the *newest* rows is the exception: what remains is still a valid chain. That is caught only by an **anchor**, a `last_hash` you wrote down earlier (see below).

## Check the chain

Run from the repo folder, with `STATE_DB_URL` set to the database:

```powershell
python audit_store.py verify
```

It prints `{"ok": true, "rows": N, "first_bad_seq": null, "last_hash": "<64 hex>"}` and exits 0. If the chain is broken, it exits 1 and names the first bad `seq`.

`verify` only reads. It takes no locks, changes nothing, and is safe against the live database. It does load every row into memory.

**Keep an anchor.** Each time you check the chain or take a dump, write the date and `last_hash` down somewhere outside the database, for example in your notes. Later:

```powershell
python audit_store.py verify --anchor <that last_hash>
```

This fails with `"anchor_found": false` if that row, and with it everything after it, has been removed.

## Back up

**Neon Free keeps a 6-hour history window**, capped at 1 GB of changes. Within that window the console can restore the whole database to an earlier moment (the project's **Backup & Restore** page; Neon has renamed console pages before, so look for "restore"). For anything older you need your own copy.

Your own copy needs the PostgreSQL client tools (`pg_dump`, `pg_restore`) on the laptop. Installing them is your call; the official installer from postgresql.org has a "Command Line Tools" option. Two requirements:

- The client's major version must be at least the server's (Neon shows it on the project's dashboard). Otherwise `pg_dump` stops.
- Use the **direct** connection string, not the `-pooler` one.

```powershell
$s = Read-Host "Neon direct connection string" -AsSecureString   # not shown, not saved
$env:PGURL = [System.Net.NetworkCredential]::new("", $s).Password
pg_dump --format=custom --no-owner --dbname=$env:PGURL --file="gca-state-$(Get-Date -Format yyyy-MM-dd).dump"
Remove-Item Env:PGURL
```

Then run `verify` and note `last_hash` beside the dump's date: that is the dump's anchor.

Keep the dump off the repo and off any public place. It holds approval reasons, key ids and client addresses.

## Restore

1. Create a **new, empty database**. In the Neon console: **Databases -> New database**, in the same project.
   Do **not** use a new branch: a branch is a copy of its parent, not empty. Restoring into it would add the dump's rows on top of the copied ones and break the chain.
2. Restore the dump into it:

   ```powershell
   pg_restore --no-owner --dbname="<the new database's direct connection string>" gca-state-YYYY-MM-DD.dump
   ```

3. Point `STATE_DB_URL` at the new database and run `python audit_store.py verify --anchor <the dump's last_hash>`. It must say `"ok": true`.
4. Put the new connection string in Render's `STATE_DB_URL` (owner step 14, part 4).

The restore brings back the triggers with the table, so the restored copy is append-only at once.

## Restore test log

| Date (UTC) | Where | Rows | Result |
|---|---|---|---|
| 2026-11-24 | Local Postgres 16.15 (this repo's test container), not Neon | 50, written in two batches | `pg_dump --format=custom` (7,210 bytes), then `pg_restore --no-owner` into an empty database. `verify`: `{"ok": true, "rows": 50, "first_bad_seq": null}`. A `DELETE` on the restored table was refused by `audit_events_append_only()`. |

A restore test against the real Neon database waits for owner step 14. Add a row here when it is done.

## What this does not protect against

- **Anyone holding the app's own `STATE_DB_URL`** (or the Neon owner's password). The app's role owns the table, so it can switch the triggers off, drop the table, or rewrite every row and recompute the whole chain. The chain only catches an edit by someone who did not recompute it. A rewrite is caught by comparing against a dated off-site dump or an anchor written down outside the database. A separate INSERT-only role for the app would close this; Neon Free allows extra roles, but the app currently creates its own table, so that change needs a migration step (a later item).
- **Removing the newest rows without an anchor.** Without a `last_hash` kept elsewhere, a shortened chain looks valid.
- **Events the writer never wrote.** If the background writer can't reach the database, it counts every lost event (`/metrics`, item 63) and the JSONL still has them; the table does not. The same goes for events still queued when the process is stopped, which are counted after a 5-second wait. An event whose write was retried after an unclear commit can appear twice, so dedupe on `event_id` when reading.
