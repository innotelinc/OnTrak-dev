from __future__ import annotations

import json
from pathlib import Path

import pytest

from ontrak.models import Category
from ontrak.scenarios import ScenarioError, ScenarioRepository, normalise_category


def test_every_shipped_scenario_validates(repo):
    problems = repo.validate()
    assert problems == [], "\n".join(problems)


def test_all_five_survey_categories_are_covered(repo):
    categories = {scenario.category for scenario in repo.list()}
    assert categories == {c.value for c in Category}


def test_objectives_are_reported_by_their_check_script(repo):
    for scenario in repo.list():
        check_text = Path(scenario.check_script).read_text()
        for objective in scenario.objectives:
            assert f"'{objective.id}'" in check_text or f'"{objective.id}"' in check_text


def test_scenario_ids_match_their_directory(repo):
    for scenario in repo.list():
        assert scenario.id == scenario.directory.name


def test_get_unknown_scenario_raises(repo):
    with pytest.raises(ScenarioError, match="unknown scenario"):
        repo.get("does-not-exist")


def test_hints_reveal_progressively(repo):
    scenario = repo.get("sec-malware-persistence")
    assert scenario.hints_up_to(0) == []
    assert scenario.hints_up_to(1) == scenario.hints[:1]
    assert scenario.hints_up_to(99) == scenario.hints


def test_public_view_hides_scripts(repo):
    scenario = repo.get("net-dns-failure")
    public = scenario.public(hint_level=2)
    assert "setup.ps1" not in str(public)
    assert public["hint_count"] == len(scenario.hints)
    assert len(public["hints_revealed"]) == 2
    assert {o["id"] for o in public["objectives"]} == {o.id for o in scenario.objectives}


def test_weights_and_critical_flags_are_sane(repo):
    for scenario in repo.list():
        assert scenario.objectives
        assert all(o.weight > 0 for o in scenario.objectives)
        assert sum(o.weight for o in scenario.objectives) == 100, (
            f"{scenario.id} weights should total 100 for a predictable pass mark"
        )
        assert 0 < len(scenario.critical_objectives) < len(scenario.objectives)


def test_hardware_scenario_declares_its_extra_device(repo):
    scenario = repo.get("hw-driver-device")
    assert scenario.instance_devices, "the device scenario needs a second NIC"
    device = scenario.instance_devices[0]
    assert device["type"] == "nic" and device["network"]


def test_validate_catches_a_broken_scenario(tmp_path):
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "scenario.yaml").write_text(
        """
id: broken
title: Broken
category: network
briefing: nothing here
objectives:
  - id: missing-check
    text: never reported
    weight: 10
""".strip()
    )
    (broken / "setup.ps1").write_text("# no marker\n")
    (broken / "check.ps1").write_text("# no report call\n")
    problems = ScenarioRepository(tmp_path).validate()
    joined = "\n".join(problems)
    assert "setup.ps1 is missing or empty" not in joined  # it exists, just wrong
    assert "ONTRAK-SETUP-OK" in joined
    assert "Write-OnTrakReport" in joined
    assert "never reports objective 'missing-check'" in joined


def test_category_aliases():
    assert normalise_category("Drivers") == Category.HARDWARE.value
    assert normalise_category("Malware") == Category.SECURITY.value
    assert normalise_category("perf") == Category.OS.value
    with pytest.raises(ScenarioError):
        normalise_category("interpretive-dance")


def test_by_category_groups_everything(repo):
    grouped = repo.by_category()
    assert sum(len(v) for v in grouped.values()) == len(repo.list())


def test_the_public_view_never_carries_the_ticket_rubric(repo):
    """The reported bug: the session page printed the form beside the student.

    ``ticket:`` holds both the request's header (who reported it, on what) and
    ``form:`` — the field list with its weights, hints and the terms a competent
    answer must contain (`all_of: [750]`, `min_words: 6`). The page rendered the
    block as a key/value table, so the student saw the answers in the very card
    they were meant to answer from.
    """
    scenario = repo.get("linux-dir-tree-build")
    assert scenario.ticket.get("form"), "the fixture scenario must declare a form"

    public = scenario.public()
    assert public["ticket"], "the header should still reach the page"
    assert set(public["ticket"]) == {"From", "System", "Priority", "Channel", "Reported"}
    assert public["ticket"]["From"] == "Dana Okafor (Platform team)"

    # Structural, not a word search: "form" is a substring of "platform" in the
    # briefing, and `weight` is a legitimate key on an objective. What must never
    # arrive is the rubric itself — the field spec, its terms or its hints.
    assert all(isinstance(v, str) for v in public["ticket"].values())
    rendered = json.dumps(public["ticket"])
    for leak in ("min_words", "all_of", "any_of", "hint", "fields", "title"):
        assert leak not in rendered, f"{leak!r} leaked into the ticket header"
    assert "Change record" not in rendered, "the form's own title leaked"


def test_a_ticket_header_drops_everything_that_is_not_a_label(repo):
    scenario = repo.get("linux-dir-tree-build")
    header = scenario.ticket_header
    assert header["System"] == "build-02 (Ubuntu)"
    assert "Form" not in header
    # A nested structure cannot become a table cell, whatever key it arrives under.
    assert all(isinstance(value, str) for value in header.values())


def test_a_scenario_without_a_ticket_reports_an_empty_header(repo):
    # A scenario written before the ticket system has no block at all, so the
    # header must be empty rather than raise: the session page calls it for every
    # scenario, ticket or not.
    scenario = repo.get("linux-dir-tree-build")
    scenario.ticket = {}
    assert scenario.ticket_header == {}
    assert scenario.public()["ticket"] == {}
