"""
fetch_fhir_gold.py
==================
Fetch the prebuilt FHIR clinical Gold database for a deployment.

WHY THIS EXISTS
The FHIR Gold (`medallion/fhir_gold.duckdb`, ~43 MB) is a derived artifact and is
gitignored, so a fresh deploy has no clinical dataset and the UI's dataset
switcher shows "Clinical records" as unavailable. The obvious fix -- rebuild it
during the deploy -- is a bad trade:

    fetch_synthea.py   downloads a few hundred MB and expands to ~1.4 GB
    build_fhir_gold.py parses 1,180 bundles into ~367k resources

That is minutes of build time and gigabytes of transient disk on every single
deploy, to produce a 43 MB file that only changes when the build logic changes
(the Synthea sample itself is a fixed Sep-2019 snapshot -- it does not move).

So: build it ONCE, publish it as a release asset, and have each deploy download
the finished 43 MB file. Same artifact, seconds instead of minutes.

USAGE
    python scripts/fetch_fhir_gold.py                 # uses $FHIR_GOLD_URL
    python scripts/fetch_fhir_gold.py --url https://...
    python scripts/fetch_fhir_gold.py --check         # report, download nothing

EXIT CODES
    0  the Gold is in place (downloaded now, or already present)
    0  FHIR_GOLD_URL is unset -- SKIPPED, deliberately not an error, so a deploy
       that only wants the hospital dataset still succeeds
    1  a URL was given but the download failed or looked wrong

PUBLISHING A NEW COPY (only needed when the build logic changes)
    python fetch_synthea.py && python build_fhir_gold.py
    gh release create fhir-gold-YYYYMMDD medallion/fhir_gold.duckdb \
        --title "FHIR Gold (Synthea Sep-2019)" \
        --notes "Prebuilt clinical Gold. Set FHIR_GOLD_URL to this asset's URL."
    # then point FHIR_GOLD_URL at the asset's browser_download_url
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request

DEST = os.environ.get("FHIR_GOLD_DB", os.path.join("medallion", "fhir_gold.duckdb"))

# A DuckDB file starts with a 4-byte magic at offset 8 ("DUCK"). Checking it
# turns "we downloaded an HTML error page and saved it as a database" into a
# clear failure here rather than a confusing IOException at app startup.
DUCKDB_MAGIC_OFFSET = 8
DUCKDB_MAGIC = b"DUCK"
MIN_PLAUSIBLE_BYTES = 1_000_000       # the real artifact is ~43 MB
DOWNLOAD_TIMEOUT_SEC = 60             # per-read timeout; see download()

# Expected SHA-256 of each published Gold asset (audit item 51). A download
# whose hash differs is refused, so a replaced or tampered asset never lands on
# the deploy. FHIR_GOLD_SHA256 overrides (for a newly published asset); a URL
# with no known hash is used with a loud warning that prints its hash to pin.
PUBLISHED_SHA256 = {
    "https://github.com/erjonb19/governed-clinical-agent/releases/download/"
    "fhir-gold-20260901/fhir_gold.duckdb":
        "606cd78992d821a8eed95a2d67a3670383c5197d6b558b6f18ce713649b79fda",
}


def expected_sha256(url: str) -> str | None:
    pinned = (os.environ.get("FHIR_GOLD_SHA256") or "").strip().lower()
    return pinned or PUBLISHED_SHA256.get(url)


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _looks_like_duckdb(path: str) -> tuple[bool, str]:
    size = os.path.getsize(path)
    if size < MIN_PLAUSIBLE_BYTES:
        return False, f"file is only {size/1e6:.2f} MB -- too small to be the Gold"
    with open(path, "rb") as fh:
        fh.seek(DUCKDB_MAGIC_OFFSET)
        magic = fh.read(len(DUCKDB_MAGIC))
    if magic != DUCKDB_MAGIC:
        return False, (f"not a DuckDB file (magic {magic!r} at byte "
                       f"{DUCKDB_MAGIC_OFFSET}, expected {DUCKDB_MAGIC!r}) -- "
                       "the URL probably served an error page or a redirect")
    return True, f"{size/1e6:.1f} MB, DuckDB magic OK"


def download(url: str, dest: str) -> None:
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    print(f"fetching FHIR Gold\n  from {url}\n  to   {dest}")

    # Download to a temp file and only move it into place once it validates, so
    # a failed or truncated download can never leave a half-written database
    # where the app will try to open it.
    fd, tmp = tempfile.mkstemp(suffix=".duckdb.part",
                               dir=os.path.dirname(dest) or ".")
    os.close(fd)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        # An explicit timeout matters more here than it looks. render.yaml chains
        # this with `&&`, so a connection that opens but then stalls -- a common
        # CDN/proxy failure -- would hang the whole build behind a frozen progress
        # line until the platform's build timeout kills it, with nothing in the log
        # saying why. Fail fast and loudly instead.
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SEC) as resp, \
                open(tmp, "wb") as out:
            total = 0
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                total += len(chunk)
                print(f"  {total/1e6:.0f} MB", end="\r")
        print()

        ok, detail = _looks_like_duckdb(tmp)
        if not ok:
            sys.exit(f"ERROR: downloaded file is not usable -- {detail}")
        got, want = sha256_of(tmp), expected_sha256(url)
        if want is None:
            print(f"WARNING: no expected SHA-256 for this URL; got {got}. Pin it in "
                  "PUBLISHED_SHA256 (scripts/fetch_fhir_gold.py) or FHIR_GOLD_SHA256.")
        elif got != want:
            sys.exit(f"ERROR: SHA-256 mismatch for {url}\n  expected {want}\n  got      {got}\n"
                     "  The asset changed. Refusing it; publish a new release and pin its hash.")
        else:
            detail += ", SHA-256 verified"

        shutil.move(tmp, dest)
        print(f"OK: FHIR Gold in place ({detail})")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    ap.add_argument("--url", default=os.environ.get("FHIR_GOLD_URL"),
                    help="URL of the prebuilt fhir_gold.duckdb (default: $FHIR_GOLD_URL)")
    ap.add_argument("--dest", default=DEST, help=f"where to write it (default: {DEST})")
    ap.add_argument("--force", action="store_true",
                    help="re-download even if a Gold is already present")
    ap.add_argument("--check", action="store_true",
                    help="report what is present and exit; download nothing")
    args = ap.parse_args()

    if args.check:
        if os.path.exists(args.dest):
            ok, detail = _looks_like_duckdb(args.dest)
            print(f"{args.dest}: {'OK' if ok else 'INVALID'} -- {detail}")
        else:
            print(f"{args.dest}: ABSENT")
        print(f"FHIR_GOLD_URL: {args.url or '(unset)'}")
        return

    if os.path.exists(args.dest) and not args.force:
        ok, detail = _looks_like_duckdb(args.dest)
        if ok:
            print(f"FHIR Gold already present ({detail}); nothing to do. "
                  f"Use --force to re-download.")
            return
        print(f"WARNING: existing {args.dest} looks wrong ({detail}); replacing it.")

    if args.url:
        # A mistyped or expired FHIR_GOLD_URL is the likeliest failure here, and a
        # bare traceback in a build log is a poor way to learn that. Name the cause.
        try:
            download(args.url, args.dest)
        except urllib.error.HTTPError as e:
            sys.exit(f"ERROR: {e.code} {e.reason} fetching {args.url}\n"
                     f"  Check FHIR_GOLD_URL points at a published release asset.")
        except urllib.error.URLError as e:
            sys.exit(f"ERROR: could not reach {args.url}: {e.reason}")
        except TimeoutError:
            sys.exit(f"ERROR: timed out after {DOWNLOAD_TIMEOUT_SEC}s fetching {args.url}")
        return

    if not args.url:
        # Deliberately exit 0. A deploy that serves only the hospital dataset is
        # a valid configuration, and the app already degrades correctly: app.py
        # checks os.path.exists per dataset and /datasets reports
        # "available": false, so the UI disables the option instead of erroring.
        print("FHIR_GOLD_URL is not set -- SKIPPING the clinical dataset.\n"
              "  This deploy will serve the hospital dataset only, which is a\n"
              "  supported configuration. To enable the clinical dataset, publish\n"
              "  medallion/fhir_gold.duckdb as a release asset and set\n"
              "  FHIR_GOLD_URL to its download URL (see this file's header).")
        return


if __name__ == "__main__":
    main()
