"""user_keys.py -- per-user API keys and roles (backlog item 22).

Until now every caller held the one shared API_KEY, so a reviewer was only a
typed name. Now each person can have their own key with a role:

    viewer   < analyst  < reviewer < admin
    findings,  asks       decides     everything (traces, metrics, the
    schema     questions  approvals   approval queue's full history)

Each role can do everything the roles before it can. Which route needs which
role is one table in app.py (`ROUTE_ROLES`); a route missing from it needs
admin (fail closed).

Keys follow docs/SECURITY_STANDARDS.md 5.3:

* made with `secrets` (a CSPRNG): `gca_<12 hex id>_<43 url-safe chars>`, 256 bits;
* only the SHA-256 of the secret part is stored (a random 256-bit secret needs
  no slow hash; a password would), compared in constant time;
* revocation is immediate: every request reads the key's row, there is no cache;
* the full key is written once, when it is made, to a new file only the admin can read
  (`deliver`: 0600, never overwritten) and never printed, stored here or logged.

The table is `api_keys` in the state database (state_db: Postgres with
STATE_DB_URL, which survives a redeploy; else the SQLite file KEYS_DB). Keys are
managed from the command line only, never over HTTP:

    python user_keys.py create <user> <role>     # the key goes to ~/.gca/keys/<user>-<id>.key
    python user_keys.py list
    python user_keys.py rotate <key_id>      # new key, same user and role; old one revoked
    (--out DIR before the command writes the key file to DIR instead; KEY_DIR sets the default)
    python user_keys.py revoke <key_id>

The deploy's API_KEY keeps working as the owner's key (role admin). With it,
names in /propose and /approvals stay typed and are recorded as
"self-asserted"; with a user key, the key's user IS the proposer or reviewer,
whatever the request body says, recorded as "key".
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

ROLES = ("viewer", "analyst", "reviewer", "admin")
RANK = {r: i for i, r in enumerate(ROLES)}
KEYS_DB = os.environ.get("KEYS_DB", "logs/api_keys.sqlite")
_KEY = re.compile(r"gca_([0-9a-f]{12})_([A-Za-z0-9_-]{43})")
_USER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")


@dataclass(frozen=True)
class Identity:
    user: str
    role: str
    key_id: str  # "" for the owner's API_KEY
    source: str  # "key" (a user key) | "owner-key" (API_KEY) | "dev" (no key configured)

    def allows(self, role: str) -> bool:
        return RANK[self.role] >= RANK[role]


def new_material() -> tuple[str, str]:
    """A fresh key id and secret, from a CSPRNG: 48 bits of id, 256 of secret."""
    return secrets.token_hex(6), secrets.token_urlsafe(32)


def looks_like_user_key(presented: Optional[str]) -> bool:
    return bool(presented) and _KEY.fullmatch(presented or "") is not None


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class KeyStore:
    def __init__(self, path: Optional[str] = None):
        self.path = path or KEYS_DB
        self._ready = False

    def _con(self) -> Any:
        import state_db

        con_cm = state_db.connect(self.path)
        return con_cm

    def _ensure(self, con: Any) -> None:
        if self._ready:
            return
        con.execute(
            "CREATE TABLE IF NOT EXISTS api_keys ("
            "key_id TEXT PRIMARY KEY, user_name TEXT NOT NULL, role TEXT NOT NULL, "
            "key_hash TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT)"
        )
        self._ready = True

    def create(
        self, user: str, role: str, key_id: Optional[str] = None, secret: Optional[str] = None
    ) -> tuple[str, str]:
        """A new key for `user`: (key_id, the full key -- never stored). The CLI
        passes key material it made first (new_material), so the key's file
        exists before the key does (review of #179, N1)."""
        if role not in RANK:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        if not _USER.fullmatch(user or ""):
            raise ValueError(
                "user: 1-64 of letters, digits and . _ @ -, starting with a letter or digit"
            )
        if key_id is None or secret is None:
            key_id, secret = new_material()
        with self._con() as con:
            self._ensure(con)
            con.execute(
                "INSERT INTO api_keys (key_id, user_name, role, key_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key_id, user, role, _digest(secret), datetime.now(timezone.utc).isoformat()),
            )
        return key_id, f"gca_{key_id}_{secret}"

    def revoke(self, key_id: str) -> bool:
        with self._con() as con:
            self._ensure(con)
            row = con.execute(
                "SELECT revoked_at FROM api_keys WHERE key_id = ?", (key_id,)
            ).fetchone()
            if row is None or row["revoked_at"]:
                return False
            con.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE key_id = ?",
                (datetime.now(timezone.utc).isoformat(), key_id),
            )
        return True

    def get(self, key_id: str) -> Optional[dict[str, Any]]:
        """One key's row, without its hash."""
        with self._con() as con:
            self._ensure(con)
            row = con.execute(
                "SELECT key_id, user_name, role, created_at, revoked_at FROM api_keys "
                "WHERE key_id = ?",
                (key_id,),
            ).fetchone()
        return dict(row) if row else None

    def rotate(self, key_id: str) -> tuple[str, str]:
        with self._con() as con:
            self._ensure(con)
            row = con.execute(
                "SELECT user_name, role, revoked_at FROM api_keys WHERE key_id = ?", (key_id,)
            ).fetchone()
        if row is None or row["revoked_at"]:
            raise ValueError(f"no active key {key_id}")
        new = self.create(row["user_name"], row["role"])
        self.revoke(key_id)
        return new

    def list(self) -> list[dict[str, Any]]:
        """Every key, without its hash."""
        with self._con() as con:
            self._ensure(con)
            rows = con.execute(
                "SELECT key_id, user_name, role, created_at, revoked_at FROM api_keys "
                "ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def has_active(self, user: str, role: str) -> bool:
        """Whether `user` holds an active key with at least `role`."""
        with self._con() as con:
            self._ensure(con)
            rows = con.execute(
                "SELECT role FROM api_keys WHERE user_name = ? AND revoked_at IS NULL", (user,)
            ).fetchall()
        return any(RANK.get(r["role"], -1) >= RANK[role] for r in rows)

    def lookup(self, presented: Optional[str]) -> Optional[Identity]:
        """See _lookup; retried once after re-creating the table, which two
        first requests can race to create (review of #179, N8)."""
        try:
            return self._lookup(presented)
        except Exception:
            self._ready = False
            return self._lookup(presented)

    def _lookup(self, presented: Optional[str]) -> Optional[Identity]:
        """The identity a presented key belongs to, or None (unknown, malformed
        or revoked). Raises if the store cannot be read: the caller refuses."""
        m = _KEY.fullmatch(presented or "")
        if not m:
            return None
        key_id, secret = m.group(1), m.group(2)
        with self._con() as con:
            self._ensure(con)
            row = con.execute(
                "SELECT user_name, role, key_hash, revoked_at FROM api_keys WHERE key_id = ?",
                (key_id,),
            ).fetchone()
        # compare even when there is no row, so a miss takes the same time
        stored = row["key_hash"] if row else _digest("no-such-key")
        good = hmac.compare_digest(_digest(secret), stored)
        if row is None or not good or row["revoked_at"] or row["role"] not in RANK:
            return None
        return Identity(user=row["user_name"], role=row["role"], key_id=key_id, source="key")


KEY_DIR = os.environ.get("KEY_DIR", os.path.join(os.path.expanduser("~"), ".gca", "keys"))


def _key_folder(out_dir: Optional[str]) -> str:
    folder = KEY_DIR if out_dir is None else out_dir
    if not folder:
        raise ValueError("--out needs a folder")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    if os.name == "posix" and os.stat(folder).st_mode & 0o077:
        # an existing shared folder: the file is still 0600, but another user
        # could delete or swap it before it is handed over
        print(
            f"warning: {folder} is readable or writable by others; prefer a private folder",
            file=sys.stderr,
        )
    return folder


def _reserve(user: str, key_id: str, out_dir: Optional[str]) -> tuple[str, int]:
    """Create the key's file, empty, before the key exists: O_EXCL refuses any
    existing name (a planted symlink included), mode 0600."""
    if not _USER.fullmatch(user or ""):  # the name goes into a file name
        raise ValueError("user name is not valid for a key file")
    path = os.path.join(_key_folder(out_dir), f"{user}-{key_id}.key")
    return path, os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)


def deliver(user: str, key_id: str, key: str, out_dir: Optional[str] = None) -> str:
    """Write a key to a NEW file only its owner can read and return the path.
    The key is never printed: a terminal's scrollback or a captured log would
    keep it (CodeQL, #179). On Windows the folder's access list decides who can
    read it; the default folder under the user's profile is private."""
    path, fd = _reserve(user, key_id, out_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    return path


def issue(
    store: "KeyStore",
    user: str,
    role: str,
    out_dir: Optional[str] = None,
    replaces: Optional[str] = None,
) -> tuple[str, str]:
    """Make a key and hand it over safely: the file first, then the key in the
    store, then the key into the file, and only then (rotate) the old key
    revoked. Any failure undoes what was done, so nobody is locked out and no
    key is left active that nobody holds (review of #179, N1)."""
    key_id, secret = new_material()
    path, fd = _reserve(user, key_id, out_dir)
    try:
        store.create(user, role, key_id=key_id, secret=secret)
    except BaseException:
        os.close(fd)
        os.unlink(path)
        raise
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"gca_{key_id}_{secret}\n")
    except BaseException:
        store.revoke(key_id)
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    if replaces:
        store.revoke(replaces)
    return key_id, path


def main(argv: list[str]) -> int:
    store = KeyStore()
    args = list(argv[1:])
    out_dir = None
    if "--out" in args:
        i = args.index("--out")
        if i + 1 >= len(args):
            print("error: --out needs a folder", file=sys.stderr)
            return 2
        out_dir = args[i + 1]
        del args[i : i + 2]
    cmd, args = (args[0] if args else ""), args[1:]
    try:
        if cmd == "create" and len(args) == 2:
            key_id, path = issue(store, args[0], args[1], out_dir)
            print(f"key_id: {key_id}")
            print(f"the key is in {path} (readable by you only); give it to {args[0]} over a")
            print("private channel, then delete the file. Only its hash is kept here.")
        elif cmd == "rotate" and len(args) == 1:
            old_row = store.get(args[0])
            if old_row is None or old_row["revoked_at"]:
                raise ValueError(f"no active key {args[0]}")
            key_id, path = issue(
                store, old_row["user_name"], old_row["role"], out_dir, replaces=args[0]
            )
            print(f"old key {args[0]} revoked; new key_id: {key_id}")
            print(f"the new key is in {path} (readable by you only); delete it once handed over.")
        elif cmd == "revoke" and len(args) == 1:
            print("revoked" if store.revoke(args[0]) else f"no active key {args[0]}")
        elif cmd == "list" and not args:
            for r in store.list():
                state = f"revoked {r['revoked_at']}" if r["revoked_at"] else "active"
                print(f"{r['key_id']}  {r['user_name']:<24} {r['role']:<9} {state}")
        else:
            print(__doc__.split("\n\n")[0], file=sys.stderr)
            print(
                "usage: user_keys.py [--out DIR] create <user> <role> | list | "
                "[--out DIR] rotate <key_id> | revoke <key_id>",
                file=sys.stderr,
            )
            return 2
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
