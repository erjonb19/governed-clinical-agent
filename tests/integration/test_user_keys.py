"""Backlog item 22: per-user keys and roles.

Done when (BACKLOG): a test per role shows each refused outside its rights; a
revoked key is refused at once; the audit log names the user for every decision.
Plus: every route is either public or in ROUTE_ROLES (a new route needs admin),
the key's user -- not a typed name -- proposes and decides, and the owner's
API_KEY keeps working as admin with typed, self-asserted names.
"""

import json
import os
import sys

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

import user_keys  # noqa: E402

OWNER = {"X-API-Key": "test-key"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in {
        "API_KEY": "test-key",
        "TRACE_DB": tmp_path / "t.sqlite",
        "CHECKPOINT_DB": tmp_path / "c.sqlite",
        "APPROVALS_DB": tmp_path / "a.sqlite",
        "AUDIT_LOG_DIR": tmp_path / "audit",
        "PLANNER_METRICS_LOG": tmp_path / "m.jsonl",
    }.items():
        monkeypatch.setenv(k, str(v))
    import importlib

    import src.tools.base as tool_base
    import tracing

    saved = dict(tool_base._TOOL_REGISTRY)
    tool_base._TOOL_REGISTRY.pop("analytics.query_aggregate", None)
    tracing.set_tracer(tracing.Tracer(str(tmp_path / "t.sqlite")))
    import app as m

    importlib.reload(m)
    store = user_keys.KeyStore(str(tmp_path / "keys.sqlite"))
    try:
        with TestClient(m.app) as c:
            m._STATE["key_store"] = store
            yield m, c, store, tmp_path
    finally:
        tracing.set_tracer(None)
        tool_base._TOOL_REGISTRY.clear()
        tool_base._TOOL_REGISTRY.update(saved)


def _key(store, user, role):
    return {"X-API-Key": store.create(user, role)[1]}


# one request per role boundary: (method, path, body, least role)
PROBES = [
    ("GET", "/findings", None, "viewer"),
    ("POST", "/raw-sql", {"sql": "SELECT 1 AS n"}, "analyst"),
    ("GET", "/approvals", None, "reviewer"),
    ("GET", "/metrics", None, "admin"),
    ("GET", "/traces", None, "admin"),
]


@pytest.mark.parametrize("role", user_keys.ROLES)
def test_each_role_is_refused_outside_its_rights(env, role):
    _, c, store, _ = env
    h = _key(store, f"u-{role}", role)
    for method, path, body, needed in PROBES:
        r = c.request(method, path, headers=h, json=body)
        if user_keys.RANK[role] >= user_keys.RANK[needed]:
            assert r.status_code not in (401, 403), (role, path, r.status_code, r.text)
        else:
            assert r.status_code == 403, (role, path, r.status_code)
            assert f"needs {needed}" in r.json()["detail"]


def test_a_revoked_key_is_refused_at_once(env):
    _, c, store, _ = env
    key_id, key = store.create("rita", "analyst")
    h = {"X-API-Key": key}
    assert c.post("/raw-sql", headers=h, json={"sql": "SELECT 1 AS n"}).status_code == 200
    assert store.revoke(key_id)
    assert c.post("/raw-sql", headers=h, json={"sql": "SELECT 1 AS n"}).status_code == 401


def test_a_rotated_key_replaces_the_old_one(env):
    _, c, store, _ = env
    key_id, old = store.create("rota", "viewer")
    new_id, new = store.rotate(key_id)
    assert new_id != key_id
    assert c.get("/findings", headers={"X-API-Key": old}).status_code == 401
    assert c.get("/findings", headers={"X-API-Key": new}).status_code != 401


@pytest.mark.parametrize(
    "key",
    [
        "gca_000000000000_" + "A" * 43,  # well-formed, unknown
        "gca_short",
        "",
    ],
)
def test_unknown_or_malformed_keys_are_401(env, key):
    _, c, store, _ = env
    store.create("someone", "admin")
    r = c.get("/findings", headers={"X-API-Key": key} if key else {})
    assert r.status_code == 401


def test_a_tampered_secret_with_a_real_key_id_is_401(env):
    _, c, store, _ = env
    key_id, key = store.create("tess", "admin")
    forged = f"gca_{key_id}_" + ("B" * 43)
    assert c.get("/findings", headers={"X-API-Key": forged}).status_code == 401


def test_a_key_store_that_cannot_be_read_refuses(env, monkeypatch):
    m, c, store, _ = env
    key = store.create("dora", "admin")[1]

    def down(self, presented):
        raise OSError("connection refused")

    monkeypatch.setattr(user_keys.KeyStore, "lookup", down)
    assert c.get("/findings", headers={"X-API-Key": key}).status_code == 503


def test_only_the_hash_is_stored_and_list_shows_no_hash(env):
    _, _, store, tmp_path = env
    key_id, key = store.create("hana", "reviewer")
    secret = key.split("_", 2)[2]
    import state_db

    with state_db.connect(store.path) as con:
        row = dict(con.execute("SELECT * FROM api_keys WHERE key_id = ?", (key_id,)).fetchone())
    assert secret not in json.dumps(row) and row["key_hash"] != secret
    assert all("key_hash" not in r for r in store.list())


def test_the_owner_key_is_admin_everywhere(env):
    _, c, _, _ = env
    for method, path, body, _ in PROBES:
        assert c.request(method, path, headers=OWNER, json=body).status_code not in (401, 403)


def test_every_route_is_public_or_has_a_role(env):
    m, _, _, _ = env
    from fastapi.routing import APIRoute

    missing = []
    for r in m.app.routes:
        if not isinstance(r, APIRoute) or r.path in m.PUBLIC_ROUTES:
            continue
        for method in r.methods - {"HEAD", "OPTIONS"}:
            if (method, r.path) not in m.ROUTE_ROLES:
                missing.append((method, r.path))
    # a route left out needs admin (fail closed); list it on purpose
    assert missing == [], missing


def test_an_unlisted_route_needs_admin(env):
    m, _, _, _ = env

    class _R:
        path = "/brand-new"

    class _Req:
        method = "GET"
        scope = {"route": _R()}

    assert m._needed_role(_Req()) == "admin"


class _FakeAgent:
    def __init__(self):
        self.calls = []

    def start(self, question, proposed_by=None):
        self.calls.append(("start", proposed_by))
        return {"status": "pending_approval", "approval_id": "a1"}

    def resume(self, **kw):
        self.calls.append(("resume", kw))
        return {"status": "approved", "decided_by": kw["decided_by"], "identity": kw["identity"]}


class _FakeStore:
    def get(self, approval_id):
        return {"thread_id": "t1", "status": "pending"}


def test_the_keys_user_proposes_and_decides_whatever_the_body_says(env, monkeypatch):
    m, c, store, _ = env
    agent = _FakeAgent()
    m._STATE["approval_agent"] = agent
    m._STATE["approval_store"] = _FakeStore()
    m._STATE["planner"] = None
    monkeypatch.setattr(m, "_screen_question", lambda *a, **k: None)
    ana, rev = _key(store, "ann", "analyst"), _key(store, "rob", "reviewer")
    r = c.post("/propose", headers=ana, json={"question": "q", "proposed_by": "rob"})
    assert r.status_code == 200 and agent.calls[-1] == ("start", "ann")
    r = c.post("/approvals/a1", headers=rev, json={"decision": "approve", "decided_by": "ann"})
    assert r.status_code == 200
    kw = agent.calls[-1][1]
    assert kw["decided_by"] == "rob" and kw["identity"] == "key"


def test_the_owner_key_keeps_typed_self_asserted_names(env):
    m, c, _, _ = env
    agent = _FakeAgent()
    m._STATE["approval_agent"] = agent
    m._STATE["approval_store"] = _FakeStore()
    r = c.post("/approvals/a1", headers=OWNER, json={"decision": "approve", "decided_by": "e.r"})
    assert r.status_code == 200
    kw = agent.calls[-1][1]
    assert kw["decided_by"] == "e.r" and kw["identity"] is None


def test_an_analyst_cannot_decide_an_approval(env):
    m, c, store, _ = env
    m._STATE["approval_agent"] = _FakeAgent()
    m._STATE["approval_store"] = _FakeStore()
    r = c.post(
        "/approvals/a1",
        headers=_key(store, "al", "analyst"),
        json={"decision": "approve", "decided_by": "al"},
    )
    assert r.status_code == 403


def test_the_audit_log_names_the_user(env):
    m, c, store, tmp_path = env
    h = _key(store, "aud", "analyst")
    assert c.post("/raw-sql", headers=h, json={"sql": "SELECT 1 AS n"}).status_code == 200
    rt = m._STATE.get("runtime")
    if rt is not None and getattr(rt, "audit_logger", None) is not None:
        rt.audit_logger.flush()
    lines = [
        json.loads(line)
        for p in (tmp_path / "audit").glob("*.jsonl")
        for line in p.read_text().splitlines()
    ]
    assert lines, "no audit events written"
    named = [e for e in lines if (e.get("principal") or "").startswith("user:aud:")]
    assert named, {e.get("principal") for e in lines}
    assert all(not (e.get("principal") or "").startswith("api:") for e in named)


def test_the_cli_creates_lists_and_revokes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(user_keys, "KEYS_DB", str(tmp_path / "cli.sqlite"))
    monkeypatch.setattr(user_keys, "KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.delenv("STATE_DB_URL", raising=False)
    assert user_keys.main(["user_keys.py", "create", "cli.user", "viewer"]) == 0
    out = capsys.readouterr().out
    key_id = out.split("key_id: ")[1].split()[0]
    # the key is never printed: it is in a new file only the admin can read
    assert "gca_" not in out
    key_file = tmp_path / "keys" / f"cli.user-{key_id}.key"
    key = key_file.read_text().strip()
    assert key.startswith(f"gca_{key_id}_") and str(key_file) in out
    if os.name == "posix":
        assert key_file.stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "keys").stat().st_mode & 0o777 == 0o700
    assert user_keys.KeyStore().lookup(key).user == "cli.user"
    assert user_keys.main(["user_keys.py", "list"]) == 0
    assert "cli.user" in capsys.readouterr().out
    # rotate writes a new file; --out picks the folder
    assert user_keys.main(["user_keys.py", "--out", str(tmp_path / "o"), "rotate", key_id]) == 0
    out = capsys.readouterr().out
    assert "gca_" not in out and len(list((tmp_path / "o").iterdir())) == 1
    assert user_keys.KeyStore().lookup(key) is None  # the old key is revoked
    assert user_keys.main(["user_keys.py", "create", "x", "superuser"]) == 2
    assert user_keys.main(["user_keys.py", "create", "bad name!", "viewer"]) == 2


def test_a_key_file_is_never_overwritten(tmp_path):
    p = user_keys.deliver("u", "abc", "gca_first", str(tmp_path))
    with pytest.raises(FileExistsError):
        user_keys.deliver("u", "abc", "gca_second", str(tmp_path))
    assert open(p).read().strip() == "gca_first"


# ---- review of #179 -------------------------------------------------------


class _RealStoreAgent:
    """Resumes by recording the decision in a REAL ApprovalStore, as the graph does."""

    def __init__(self, store):
        self.store = store

    def resume(
        self,
        thread_id,
        decision,
        decided_by,
        reason=None,
        edited_proposal=None,
        escalated_to=None,
        identity=None,
    ):
        aid = self.store.find_by_thread(thread_id, status="pending")
        return self.store.decide(
            aid,
            decision,
            decided_by,
            reason=reason,
            edited_proposal=edited_proposal,
            escalated_to=escalated_to,
            identity=identity,
        )


def _real_queue(m, tmp_path):
    from approval import ApprovalStore

    store = ApprovalStore(str(tmp_path / "real_approvals.sqlite"))
    aid = store.propose("t-real", "brief.commit", "q?", "a brief", proposed_by="ann")
    m._STATE["approval_store"] = store
    m._STATE["approval_agent"] = _RealStoreAgent(store)
    return store, aid


def test_a_key_decision_is_stored_as_key_and_audited_under_the_user(env):
    m, c, keys, tmp_path = env
    store, aid = _real_queue(m, tmp_path)
    rev = _key(keys, "rob", "reviewer")
    r = c.post(f"/approvals/{aid}", headers=rev, json={"decision": "approve", "decided_by": "ann"})
    assert r.status_code == 200, r.text
    hist = store.history(aid)
    assert hist[-1]["decided_by"] == "rob" and hist[-1]["identity"] == "key"
    rt = m._STATE.get("runtime")
    if rt is not None and getattr(rt, "audit_logger", None) is not None:
        rt.audit_logger.flush()
    events = [
        json.loads(line)
        for p in (tmp_path / "audit").glob("*.jsonl")
        for line in p.read_text().splitlines()
    ]
    decisions = [e for e in events if e.get("event_type") == "approval_decision"]
    assert decisions and decisions[-1]["principal"].startswith("user:rob:")


def test_the_proposer_cannot_decide_with_their_own_key(env):
    m, c, keys, tmp_path = env
    _, aid = _real_queue(m, tmp_path)  # proposed by "ann"
    r = c.post(
        f"/approvals/{aid}",
        headers=_key(keys, "ann", "reviewer"),
        json={"decision": "approve", "decided_by": "someone-else"},
    )
    assert r.status_code == 400 and "proposed it" in r.text


@pytest.mark.parametrize("target, ok", [("vera", True), ("vic", False), ("nobody", False)])
def test_escalation_must_name_an_active_reviewer(env, target, ok):
    m, c, keys, tmp_path = env
    _, aid = _real_queue(m, tmp_path)
    keys.create("vera", "reviewer")
    keys.create("vic", "viewer")
    r = c.post(
        f"/approvals/{aid}",
        headers=_key(keys, "rob", "reviewer"),
        json={"decision": "escalate", "escalated_to": target},
    )
    assert (r.status_code == 200) is ok, (target, r.status_code, r.text)


def test_roles_hold_on_v1_paths(env):
    _, c, keys, _ = env
    viewer = _key(keys, "vee", "viewer")
    assert c.get("/v1/findings", headers=viewer).status_code not in (401, 403)
    assert c.get("/v1/approvals", headers=viewer).status_code == 403
    assert c.post("/v1/raw-sql", headers=viewer, json={"sql": "SELECT 1 AS n"}).status_code == 403


def test_a_non_ascii_key_is_a_counted_401_not_a_500(env):
    _, c, _, _ = env
    import auth_failures

    before = auth_failures.FAILURES.total
    r = c.get("/findings", headers={"X-API-Key": b"t\xc3\xa9st-key"})
    assert r.status_code == 401
    assert auth_failures.FAILURES.total == before + 1


def test_two_key_headers_are_refused(env):
    _, c, keys, _ = env
    k = keys.create("dup", "admin")[1]
    r = c.get("/findings", headers=[("X-API-Key", "test-key"), ("X-API-Key", k)])
    assert r.status_code == 400


def test_one_key_lookup_per_request(env, monkeypatch):
    m, c, keys, _ = env
    h = _key(keys, "once", "analyst")
    calls = []
    real = user_keys.KeyStore.lookup

    def counting(self, presented):
        calls.append(1)
        return real(self, presented)

    monkeypatch.setattr(user_keys.KeyStore, "lookup", counting)
    assert c.post("/raw-sql", headers=h, json={"sql": "SELECT 1 AS n"}).status_code == 200
    assert len(calls) == 1
