"""
fetch_synthea.py
================
Downloads the Synthea FHIR R4 sample dataset -- ~1,180 synthetic patients, each
as a nested FHIR transaction Bundle containing a few hundred resources.

Why this data: it is RAW, properly-nested FHIR R4, structurally the same thing a
real Epic/Cerner bulk export produces. The engineering value is not in
generating it -- it is in flattening deeply nested bundles into queryable
relational tables, resolving references across resources, and handling coded
values (SNOMED, LOINC, RxNorm) correctly. Pre-flattened CSVs would skip exactly
the part that matters.

Structure to expect (per Synthea docs):
  - one JSON file per patient, Bundle type "transaction"
  - the Patient resource is the FIRST entry
  - then patient-specific resources (Encounter, Condition, Observation,
    Procedure, MedicationRequest, ...) roughly grouped by Encounter in
    chronological order
  - Organization and Practitioner are exported SEPARATELY, because multiple
    patients reference them -- so those are cross-bundle references the
    flattener must resolve

Usage:
    python fetch_synthea.py            # download + extract + inspect
    python fetch_synthea.py --inspect  # just inspect what is already on disk

Output: data/synthea/fhir/*.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import zipfile
from collections import Counter

URL = ("https://synthetichealth.github.io/synthea-sample-data/"
       "downloads/synthea_sample_data_fhir_r4_sep2019.zip")
DEST_DIR = os.path.join("data", "synthea")
ZIP_PATH = os.path.join(DEST_DIR, "synthea_sample_data_fhir_r4_sep2019.zip")

# Expected SHA-256 of the fixed Sep-2019 zip (audit item 51). Not yet pinned:
# the host is unreachable from the session that wrote this check. The next
# download prints the hash; commit it here (or set SYNTHEA_SHA256), and from
# then on a different file is refused.
EXPECTED_SHA256: str | None = None


def verify(path: str = ZIP_PATH) -> str:
    """Hash the zip; exit on a mismatch with the pinned hash, if one is set."""
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    got = h.hexdigest()
    want = (os.environ.get("SYNTHEA_SHA256") or EXPECTED_SHA256 or "").strip().lower()
    if not want:
        print(f"WARNING: Synthea zip SHA-256 is not pinned; got {got}. "
              "Pin it as EXPECTED_SHA256 in fetch_synthea.py.")
    elif got != want:
        raise SystemExit(f"ERROR: Synthea zip SHA-256 mismatch\n  expected {want}\n"
                         f"  got      {got}\n  Refusing it; delete {path} and re-download.")
    else:
        print(f"Synthea zip SHA-256 verified ({got[:12]}...)")
    return got


def download() -> None:
    os.makedirs(DEST_DIR, exist_ok=True)
    if os.path.exists(ZIP_PATH) and os.path.getsize(ZIP_PATH) > 0:
        print(f"already downloaded: {ZIP_PATH} "
              f"({os.path.getsize(ZIP_PATH)/1e6:.0f} MB)")
        return
    print(f"downloading {URL}")
    print("(this is a few hundred MB -- give it a minute)")
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp, open(ZIP_PATH, "wb") as f:
        total = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
            print(f"  {total/1e6:.0f} MB", end="\r")
    print(f"\ndownloaded {total/1e6:.0f} MB -> {ZIP_PATH}")


def extract() -> str:
    """Extract and return the directory holding the patient bundles."""
    with zipfile.ZipFile(ZIP_PATH) as z:
        names = z.namelist()
        json_names = [n for n in names if n.lower().endswith(".json")]
        print(f"archive contains {len(json_names)} JSON files")
        # only extract if we do not already have them
        existing = []
        for root, _, files in os.walk(DEST_DIR):
            existing += [f for f in files if f.endswith(".json")]
        if len(existing) >= len(json_names) > 0:
            print(f"already extracted ({len(existing)} json files on disk)")
        else:
            print("extracting...")
            z.extractall(DEST_DIR)
            print("extracted")
    # find the directory with the most json files -- that is the bundle dir
    best, best_n = DEST_DIR, 0
    for root, _, files in os.walk(DEST_DIR):
        n = sum(1 for f in files if f.endswith(".json"))
        if n > best_n:
            best, best_n = root, n
    return best


def inspect(bundle_dir: str, sample: int = 25) -> None:
    """Report what is actually in the bundles -- build against reality, not guesses."""
    files = sorted(f for f in os.listdir(bundle_dir) if f.endswith(".json"))
    print(f"\nbundle dir: {bundle_dir}")
    print(f"total bundles: {len(files)}")

    # separate patient bundles from the shared Organization/Practitioner files
    special = [f for f in files if f.lower().startswith(("hospital", "practitioner"))]
    patients = [f for f in files if f not in special]
    print(f"  patient bundles: {len(patients)}")
    print(f"  shared reference files: {special if special else 'none detected'}")

    print(f"\nresource types across first {sample} patient bundles:")
    types = Counter()
    per_bundle = []
    for fn in patients[:sample]:
        with open(os.path.join(bundle_dir, fn), encoding="utf-8") as f:
            bundle = json.load(f)
        entries = bundle.get("entry", [])
        per_bundle.append(len(entries))
        for e in entries:
            rt = (e.get("resource") or {}).get("resourceType")
            if rt:
                types[rt] += 1
    for rt, n in types.most_common():
        print(f"  {rt:24} {n:6}")
    if per_bundle:
        print(f"\nresources per bundle: min={min(per_bundle)} "
              f"max={max(per_bundle)} avg={sum(per_bundle)/len(per_bundle):.0f}")

    # show the shape of one of each resource we care about, so the flattener is
    # written against real field names rather than assumptions
    want = ["Patient", "Encounter", "Condition", "Observation",
            "Procedure", "MedicationRequest"]
    print("\n--- sample resource shapes (top-level keys) ---")
    seen = {}
    for fn in patients[:sample]:
        with open(os.path.join(bundle_dir, fn), encoding="utf-8") as f:
            bundle = json.load(f)
        for e in bundle.get("entry", []):
            r = e.get("resource") or {}
            rt = r.get("resourceType")
            if rt in want and rt not in seen:
                seen[rt] = r
        if len(seen) == len(want):
            break
    for rt in want:
        r = seen.get(rt)
        if not r:
            print(f"\n{rt}: NOT FOUND in sample")
            continue
        print(f"\n{rt}: {sorted(r.keys())}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inspect", action="store_true",
                    help="skip download, just inspect what is on disk")
    args = ap.parse_args()

    if not args.inspect:
        download()
    if not os.path.exists(ZIP_PATH):
        sys.exit("no zip on disk; run without --inspect first")
    verify()   # before anything is extracted from it (audit item 51)
    bundle_dir = extract()
    inspect(bundle_dir)
    print(f"\nnext: build the flattener against these shapes")


if __name__ == "__main__":
    main()
