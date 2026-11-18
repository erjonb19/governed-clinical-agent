"""Audit item 46: every Dependabot ecosystem waits a week before proposing a
new version (a release-age cooldown; security updates are not delayed)."""

import os

import yaml

PATH = os.path.join(os.path.dirname(__file__), "..", "..", ".github", "dependabot.yml")


def test_every_ecosystem_has_a_week_long_cooldown():
    with open(PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    updates = config["updates"]
    assert {u["package-ecosystem"] for u in updates} >= {"pip", "github-actions"}
    for u in updates:
        days = (u.get("cooldown") or {}).get("default-days")
        assert isinstance(days, int) and days >= 7, (u["package-ecosystem"], days)


def test_the_base_image_stays_on_python_3_11():
    # #142 proposed 3.11 -> 3.15; the locks and Render are 3.11 (see dependabot.yml)
    with open(PATH, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    docker = next(u for u in config["updates"] if u["package-ecosystem"] == "docker")
    rules = {i["dependency-name"]: set(i.get("update-types", [])) for i in docker.get("ignore", [])}
    assert {"version-update:semver-major", "version-update:semver-minor"} <= rules.get("python", set())
