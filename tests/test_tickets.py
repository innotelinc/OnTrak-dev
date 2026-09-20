"""The in-house ticket: the write-up the student hands in with their submission.

The rubric is deliberately mechanical (length, required terms, a dropdown), so the
tests are about whether it *resists* the lazy answer while accepting every reasonable
one. Two properties matter beyond individual rules:

* every rubric shipped in the repository must be satisfiable — a form nobody can score
  full marks on is a broken ticket, and demo mode proves it in a second;
* a form is optional, so nothing changes for scenarios that predate it.
"""

from __future__ import annotations

import pytest

from ontrak.catalog import Catalog
from ontrak.demo import synthesise_ticket
from ontrak.scenarios import ScenarioRepository
from ontrak.store import Store
from ontrak.tickets import (
    MAX_TICKET_WEIGHT,
    WRITEUP_ACTION,
    TicketError,
    TicketField,
    blend,
    grade,
    load_form,
    missing_required,
    render_feedback,
    validate_form,
    word_count,
)


def form_with(fields: list[dict], weight: float = 30.0) -> object:
    return load_form({"form": {"weight": weight, "fields": fields}})


def simple_field(**overrides) -> dict:
    base = {
        "id": "cause",
        "label": "Root cause",
        "kind": "textarea",
        "weight": 100,
        "min_words": 3,
        "any_of": ["dns", "resolver"],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def test_no_form_means_no_ticket():
    assert load_form(None) is None
    assert load_form({}) is None
    assert load_form({"from": "someone", "priority": "high"}) is None


def test_a_bare_list_of_fields_is_accepted():
    form = load_form({"form": [simple_field()]})
    assert form is not None and len(form.fields) == 1


def test_field_kind_aliases_and_defaults():
    assert TicketField.from_dict({"id": "a", "kind": "multiline"}).kind == "textarea"
    assert TicketField.from_dict({"id": "b", "kind": "dropdown"}).kind == "select"
    assert TicketField.from_dict({"id": "c", "type": "text"}).kind == "text"
    # A select with no explicit answer defaults to its first option.
    field = TicketField.from_dict({"id": "d", "kind": "select", "options": ["one", "two"]})
    assert field.expected == "one"


def test_keywords_accept_scalar_list_and_csv():
    assert TicketField.from_dict({"id": "a", "keywords": "dns"}).all_of == ["dns"]
    assert TicketField.from_dict({"id": "b", "keywords": ["dns", "dhcp"]}).all_of == ["dns", "dhcp"]
    assert TicketField.from_dict({"id": "c", "keywords": "dns, dhcp"}).all_of == ["dns", "dhcp"]


def test_malformed_form_raises_ticket_error():
    with pytest.raises(TicketError):
        load_form({"form": "not a mapping or a list"})


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def test_a_healthy_form_validates():
    assert validate_form(form_with([simple_field()])) == []


def test_weights_must_total_one_hundred():
    form = form_with([simple_field(weight=60), simple_field(id="other", weight=20)])
    assert any("not 100" in problem for problem in validate_form(form))


def test_ticket_share_is_capped_so_the_machine_still_decides():
    form = form_with([simple_field()], weight=MAX_TICKET_WEIGHT + 10)
    assert any("machine state is always the larger part" in problem for problem in validate_form(form))


def test_a_free_text_field_needs_a_rubric():
    form = form_with([simple_field(required=False, min_words=0, any_of=[], all_of=[])])
    assert any("no rubric" in problem for problem in validate_form(form))


def test_duplicate_field_ids_are_reported():
    form = form_with([simple_field(), simple_field()])
    assert any("duplicate field id" in problem for problem in validate_form(form))


def test_a_field_id_may_not_take_a_portal_control_name():
    """A ticket field that shares a control name would shadow the submit buttons.

    The write-up's fields and its Save/Preview/Complete buttons are one HTML form, so
    a field called the same thing as the control is submitted in the same slot — which
    is how a field named ``action`` made *Save draft* submit the session.
    """
    form = form_with([simple_field(id=WRITEUP_ACTION)])
    assert any("reserved" in problem for problem in validate_form(form))
    assert any("reserved" in problem for problem in validate_form(form_with([simple_field(id="csrf")])))
    # The name scenarios actually use is fine: only the portal's control is reserved.
    assert validate_form(form_with([simple_field(id="action")])) == []


def test_select_without_options_is_reported():
    form = form_with(
        [{"id": "class", "label": "Class", "kind": "select", "weight": 100, "expected": "x"}]
    )
    assert any("needs options" in problem for problem in validate_form(form))


def test_empty_form_is_reported():
    assert any("no fields" in problem for problem in validate_form(load_form({"form": {"fields": []}})))


# --------------------------------------------------------------------------- #
# grading
# --------------------------------------------------------------------------- #
def test_blank_required_field_fails_and_optional_blank_passes():
    form = form_with(
        [
            {"id": "req", "label": "Required", "weight": 50, "min_words": 2, "any_of": ["x"]},
            {"id": "opt", "label": "Optional", "weight": 50, "required": False, "any_of": ["y"]},
        ]
    )
    result = grade(form, {"req": "", "opt": ""})
    outcome = {o.field_id: o.passed for o in result.outcomes}
    assert outcome == {"req": False, "opt": True}
    assert result.score == 50.0


def test_minimum_length_is_enforced():
    form = form_with([simple_field(min_words=10)])
    short = grade(form, {"cause": "dns was broken"})
    assert short.outcomes[0].passed is False
    assert "word" in short.outcomes[0].detail
    long = grade(form, {"cause": "the resolver address was set by hand and did not answer queries at all"})
    assert long.outcomes[0].passed is True


def test_any_of_accepts_a_synonym():
    form = form_with([simple_field(any_of=["dns", "name resolution", "resolver"])])
    for answer in ("the DNS server was wrong", "name resolution failed", "the resolver was misconfigured"):
        assert grade(form, {"cause": answer}).outcomes[0].passed is True


def test_all_of_demands_every_term():
    form = form_with([simple_field(all_of=["chmod", "permission"], any_of=[])])
    assert grade(form, {"cause": "changed the chmod for that file"}).outcomes[0].passed is False
    assert grade(form, {"cause": "chmod fixed the permission on the script"}).outcomes[0].passed is True


def test_rejected_answers_are_caught():
    form = form_with([simple_field(none_of=["chmod 777", "reinstall"])])
    result = grade(form, {"cause": "dns was odd so I ran chmod 777 on the directory"})
    assert result.outcomes[0].passed is False
    assert "rejected answer" in result.outcomes[0].detail


def test_select_must_match_the_expected_option():
    form = form_with(
        [
            {
                "id": "class",
                "label": "Classification",
                "kind": "select",
                "weight": 100,
                "options": ["Permissions", "Ownership"],
                "expected": "Ownership",
            }
        ]
    )
    assert grade(form, {"class": "Ownership"}).score == 100.0
    wrong = grade(form, {"class": "Permissions"})
    assert wrong.score == 0.0
    assert "correct classification" in wrong.outcomes[0].detail


def test_score_is_a_weighted_percentage():
    form = form_with(
        [
            {"id": "a", "label": "A", "weight": 70, "min_words": 1, "any_of": ["x"]},
            {"id": "b", "label": "B", "weight": 30, "min_words": 1, "any_of": ["y"]},
        ]
    )
    assert grade(form, {"a": "x happened", "b": ""}).score == 70.0


def test_no_submission_scores_zero_and_says_so():
    form = form_with([simple_field()])
    result = grade(form, {})
    assert result.submitted is False
    assert result.score == 0.0
    assert any("no ticket was submitted" in note for note in result.notes)


def test_missing_required_lists_labels_only():
    form = form_with(
        [
            {"id": "a", "label": "Root cause", "weight": 50, "min_words": 2, "any_of": ["x"]},
            {"id": "b", "label": "Notes", "weight": 50, "required": False, "any_of": ["y"]},
        ]
    )
    assert missing_required(form, {"a": "", "b": ""}) == ["Root cause"]
    assert missing_required(form, {"a": "x", "b": ""}) == []


def test_feedback_rows_name_the_failed_field():
    form = form_with([simple_field()])
    rows = render_feedback(form, grade(form, {"cause": "no idea"}))
    assert rows[0]["passed"] is False
    assert rows[0]["weight"] == 100


def test_word_count_ignores_punctuation_and_counts_a_path_once():
    # Punctuation is not a word, and a path is one token — not three words of padding.
    assert word_count("the DNS server; /etc/resolv.conf is wrong.") == 6
    assert word_count("...") == 0


# --------------------------------------------------------------------------- #
# blending with the machine score
# --------------------------------------------------------------------------- #
def test_blend_is_the_weighted_sum():
    form = form_with([simple_field()], weight=40)
    ticket = grade(form, {"cause": "the dns server address was wrong"})
    assert ticket.score == 100.0
    assert blend(50.0, ticket, 40) == 70.0


def test_no_submission_contributes_nothing():
    assert blend(100.0, None, 30) == 70.0


def test_a_scenario_without_a_ticket_is_graded_exactly_as_before():
    assert blend(80.0, None, 0) == 80.0


def test_blend_cannot_be_pushed_above_the_cap():
    form = form_with([simple_field()], weight=100)
    ticket = grade(form, {"cause": "the dns resolver was wrong"})
    assert ticket.score == 100.0
    # 100% ticket weight is refused by validation, and blend clamps it anyway.
    assert blend(0.0, ticket, 100) == 60.0


# --------------------------------------------------------------------------- #
# the repository's own rubrics
# --------------------------------------------------------------------------- #
def test_every_shipped_rubric_is_satisfiable(settings):
    """A rubric nobody can score full marks on is a broken ticket, not a strict one."""
    repository = ScenarioRepository(settings.scenarios_dir)
    for scenario in repository.list():
        if scenario.ticket_form is None:
            continue
        answers = synthesise_ticket(scenario.ticket_form)
        result = grade(
            scenario.ticket_form, answers, scenario_id=scenario.id
        )
        assert result.score == 100.0, (scenario.id, [(o.field_id, o.detail) for o in result.outcomes])


def test_every_shipped_rubric_validates(settings):
    repository = ScenarioRepository(settings.scenarios_dir)
    catalog = Catalog(settings.catalog_dir)
    problems = repository.validate(catalog=catalog)
    assert not [p for p in problems if "ticket" in p]


def test_leafy_scenarios_without_forms_are_graded_on_machine_state_only(settings):
    repository = ScenarioRepository(settings.scenarios_dir)
    forms = [s for s in repository.list() if s.ticket_form is not None]
    assert forms, "at least some scenarios should ask for a write-up"
    assert all(s.public()["has_ticket"] for s in forms)


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #
def test_drafts_are_kept_out_of_the_marked_ticket_table(settings, store):
    """A draft is not a grade: the results-only policy applies to the write-up too."""
    session = _session(store)
    store.save_ticket_draft(session.id, {"cause": "half a thought"})
    assert store.ticket_draft(session.id) == {"cause": "half a thought"}
    assert store.list_tickets() == []
    assert store.ticket_values(session.id) == {}

    form = form_with([simple_field()])
    result = grade(form, {"cause": "the dns resolver was wrong"}, session_id=session.id)
    store.save_ticket(result, session.student)
    assert len(store.list_tickets()) == 1
    marked = store.latest_ticket(session.id)
    assert marked is not None and marked.score == 100.0
    assert store.ticket_values(session.id)["cause"].startswith("the dns")


def test_store_counts_tickets(settings, store):
    form = form_with([simple_field()])
    for index, answer in enumerate(("dns", ""), start=1):
        session = _session(store, student=f"student{index}")
        store.save_ticket(grade(form, {"cause": answer}, session_id=session.id), session.student)
    stats = store.count_tickets()
    assert stats["count"] == 2
    assert stats["submitted"] == 1


def _session(store: Store, student: str = "alice"):
    from ontrak.models import Session

    session = Session(id=None, student=student, scenario_id="net-dns-failure")
    return store.create_session(session)
