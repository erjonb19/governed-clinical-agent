"""Audit item 51: the downloaded FHIR Gold is checked against a committed
SHA-256 and refused on a mismatch, before it is moved into place."""

import functools
import hashlib
import http.server
import importlib.util
import os
import threading

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _load():
    spec = importlib.util.spec_from_file_location(
        "fetch_fhir_gold", os.path.join(ROOT, "scripts", "fetch_fhir_gold.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def served(tmp_path):
    """A local HTTP server with a 2 MB file that passes the DuckDB magic check."""
    root = tmp_path / "www"
    root.mkdir()
    body = b"\0" * 8 + b"DUCK" + os.urandom(2_000_000)
    (root / "fhir_gold.duckdb").write_bytes(body)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield (
            f"http://127.0.0.1:{srv.server_address[1]}/fhir_gold.duckdb",
            hashlib.sha256(body).hexdigest(),
        )
    finally:
        srv.shutdown()


def test_the_published_asset_has_a_pinned_hash():
    mod = _load()
    assert any("fhir-gold-20260901" in u for u in mod.PUBLISHED_SHA256)
    assert all(len(h) == 64 for h in mod.PUBLISHED_SHA256.values())


def test_a_matching_hash_is_accepted(served, tmp_path, monkeypatch):
    url, digest = served
    mod = _load()
    monkeypatch.setitem(mod.PUBLISHED_SHA256, url, digest)
    dest = tmp_path / "out" / "fhir_gold.duckdb"
    mod.download(url, str(dest))
    assert dest.exists()


def test_a_mismatch_is_refused_and_nothing_lands(served, tmp_path, monkeypatch):
    url, _ = served
    mod = _load()
    monkeypatch.setitem(mod.PUBLISHED_SHA256, url, "0" * 64)
    dest = tmp_path / "out" / "fhir_gold.duckdb"
    with pytest.raises(SystemExit) as e:
        mod.download(url, str(dest))
    assert "SHA-256 mismatch" in str(e.value)
    assert not dest.exists()
    assert not list((tmp_path / "out").glob("*.part"))


def test_the_env_override_wins(served, tmp_path, monkeypatch):
    url, _ = served
    mod = _load()
    monkeypatch.setenv("FHIR_GOLD_SHA256", "f" * 64)
    with pytest.raises(SystemExit):
        mod.download(url, str(tmp_path / "x.duckdb"))


def test_an_unknown_url_warns_with_the_hash_to_pin(served, tmp_path, capsys):
    url, digest = served
    mod = _load()
    mod.download(url, str(tmp_path / "y.duckdb"))
    assert digest in capsys.readouterr().out


def test_the_synthea_zip_is_verified_before_extraction(tmp_path, monkeypatch, capsys):
    import sys

    sys.path.insert(0, ROOT)
    import fetch_synthea

    z = tmp_path / "s.zip"
    z.write_bytes(b"not the real zip")
    digest = hashlib.sha256(b"not the real zip").hexdigest()
    assert fetch_synthea.verify(str(z)) == digest
    assert digest in capsys.readouterr().out  # unpinned: prints the hash to pin
    monkeypatch.setenv("SYNTHEA_SHA256", "0" * 64)
    with pytest.raises(SystemExit):
        fetch_synthea.verify(str(z))
    monkeypatch.setenv("SYNTHEA_SHA256", digest)
    fetch_synthea.verify(str(z))
