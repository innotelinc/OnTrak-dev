"""The in-house ticket system: the form a student fills in and hands over.

A scenario grades the *machine*; a ticket grades the *technician*. That split is
deliberate. Fixing a fault and explaining it are different skills, and a support
desk hires for the second one: the fix that nobody wrote down gets re-done next
week, and the ticket that says "restarted it, seems fine" is worse than useless.

So every scenario may declare a ``ticket.form`` in its manifest::

    ticket:
      form:
        title: Incident write-up
        weight: 30                 # share of the final grade
        fields:
          - id: root_cause
            label: Root cause
            kind: textarea
            weight: 30
            required: true
            min_words: 6
            any_of: [permission, chmod, mode]

and the student's answers are graded against a rubric (required, minimum length,
must-mention / must-mention-one-of terms, and dropdown expectations). The final
submission is a blend:

    final = machine_score * (1 - ticket.weight/100)
          + ticket_score  * (ticket.weight/100)

which is why ``ticket.weight`` is capped and why a scenario without a form grades
exactly as it did before: no form, no ticket component, nothing to fill in.

Grading is keyword-and-length based, not a language model, and it says so in the
feedback ("the write-up never mentions permissions"). That is honest — a rubric
that pretends to understand prose would be worse than one that checks for the
terms a competent answer cannot avoid.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import iso

# Field kinds the portal can render and the grader understands.
KINDS = ("text", "textarea", "select", "checkbox", "number")

MAX_TICKET_WEIGHT = 60.0
DEFAULT_TICKET_WEIGHT = 30.0

# The name of the write-up form's submitting control. It is deliberately *not*
# ``action``, which is what most scenarios call one of their own fields ("what you
# changed"). The fields and the three buttons share one HTML form, and the fields
# are serialised first, so with the control named ``action`` the value the handler
# read was the student's own prose: it matched neither ``save`` nor ``preview``,
# fell through to Complete & End, and clicking *Save draft* graded the machine and
# destroyed it. A reserved name cannot be shadowed like that, and
# :data:`RESERVED_FIELD_IDS` keeps a scenario from taking it.
WRITEUP_ACTION = "ontrak_writeup"

# Control names the portal's own forms use, which a ticket field may not take.
RESERVED_FIELD_IDS = frozenset({WRITEUP_ACTION, "csrf"})

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'./_-]*")


class TicketError(RuntimeError):
    """Raised when a ticket form is malformed or a submission cannot be graded."""


@dataclass
class TicketField:
    """One question on the ticket, with the rubric used to mark it."""

    id: str
    label: str
    kind: str = "textarea"
    weight: float = 10.0
    required: bool = True
    min_words: int = 0
    max_words: int = 0
    # Every term here must appear (case-insensitive) for the field to pass — use it
    # for things a correct answer cannot avoid saying.
    all_of: list[str] = field(default_factory=list)
    # At least one of these must appear — use it to accept synonyms.
    any_of: list[str] = field(default_factory=list)
    # Terms that must *not* appear: the classic wrong answers ("reinstall Windows",
    # "chmod 777") are worth naming explicitly.
    none_of: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)
    expected: str = ""
    hint: str = ""
    placeholder: str = ""
    rows: int = 5

    @property
    def label_or_id(self) -> str:
        return self.label or self.id

    def is_choice(self) -> bool:
        return self.kind in {"select", "checkbox"} and bool(self.options)

    @classmethod
    def from_dict(cls, data: dict) -> TicketField:
        kind = str(data.get("kind") or data.get("type") or "textarea").strip().lower()
        if kind == "multiline":
            kind = "textarea"
        if kind == "dropdown":
            kind = "select"
        options = [str(o) for o in (data.get("options") or data.get("choices") or [])]
        expected = str(data.get("expected") or data.get("answer") or "").strip()
        field = cls(
            id=str(data.get("id") or "").strip(),
            label=str(data.get("label") or data.get("question") or "").strip(),
            kind=kind,
            weight=float(data.get("weight", 10)),
            required=bool(data.get("required", True)),
            min_words=int(data.get("min_words", 0)),
            max_words=int(data.get("max_words", 0)),
            all_of=_as_terms(data.get("all_of") or data.get("keywords") or data.get("contains")),
            any_of=_as_terms(data.get("any_of") or data.get("accept")),
            none_of=_as_terms(data.get("none_of") or data.get("reject")),
            options=options,
            expected=expected,
            hint=str(data.get("hint") or "").strip(),
            placeholder=str(data.get("placeholder") or "").strip(),
            rows=int(data.get("rows", 5)),
        )
        # A select whose expected answer is in options is the common case; make the
        # grader's job explicit rather than implicit.
        if field.is_choice() and not field.expected and field.options:
            field.expected = field.options[0]
        return field

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "weight": self.weight,
            "required": self.required,
            "min_words": self.min_words,
            "max_words": self.max_words,
            "all_of": list(self.all_of),
            "any_of": list(self.any_of),
            "none_of": list(self.none_of),
            "options": list(self.options),
            "hint": self.hint,
            "placeholder": self.placeholder,
            "rows": self.rows,
        }


def _as_terms(value: Any) -> list[str]:
    """Accept ``a``, ``[a, b]`` or ``"a, b"`` for a term list."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",") if "," in value else [value]
        return [p.strip() for p in parts if p.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


@dataclass
class TicketForm:
    """The whole ticket a student has to complete."""

    title: str = "Incident write-up"
    intro: str = ""
    weight: float = DEFAULT_TICKET_WEIGHT
    fields: list[TicketField] = field(default_factory=list)
    # A short closing note the student can add (not graded, kept for the record).
    closing_label: str = "Anything else? (not graded)"
    pass_score: float = 60.0

    @property
    def total_weight(self) -> float:
        return sum(f.weight for f in self.fields)

    def field(self, field_id: str) -> TicketField | None:
        return next((f for f in self.fields if f.id == field_id), None)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "intro": self.intro,
            "weight": self.weight,
            "pass_score": self.pass_score,
            "closing_label": self.closing_label,
            "fields": [f.to_dict() for f in self.fields],
        }

    def public(self) -> dict:
        """Portal-facing view: the rubric weights are shown, the expected answers are not."""
        return {
            "title": self.title,
            "intro": self.intro,
            "weight": self.weight,
            "pass_score": self.pass_score,
            "closing_label": self.closing_label,
            "fields": [
                {
                    "id": f.id,
                    "label": f.label_or_id,
                    "kind": f.kind,
                    "weight": f.weight,
                    "required": f.required,
                    "min_words": f.min_words,
                    "max_words": f.max_words,
                    "options": list(f.options),
                    "hint": f.hint,
                    "placeholder": f.placeholder,
                    "rows": f.rows,
                }
                for f in self.fields
            ],
        }


def load_form(ticket: dict | None) -> TicketForm | None:
    """Build a :class:`TicketForm` from a scenario's ``ticket:`` block.

    Returns ``None`` when the scenario declares no form, which is what makes the
    ticket component optional rather than a new requirement on every scenario.
    """
    if not isinstance(ticket, dict):
        return None
    raw = ticket.get("form")
    if raw is None:
        return None
    if isinstance(raw, list):  # bare list of fields is the common shorthand
        raw = {"fields": raw}
    if not isinstance(raw, dict):
        raise TicketError("ticket.form must be a mapping (or a list of fields)")
    fields = [
        TicketField.from_dict(item) for item in (raw.get("fields") or []) if isinstance(item, dict)
    ]
    return TicketForm(
        title=str(raw.get("title") or "Incident write-up"),
        intro=str(raw.get("intro") or ticket.get("briefing") or "").strip(),
        weight=float(raw.get("weight", DEFAULT_TICKET_WEIGHT)),
        fields=fields,
        closing_label=str(raw.get("closing_label") or "Anything else? (not graded)"),
        pass_score=float(raw.get("pass_score", 60.0)),
    )


def validate_form(form: TicketForm | None, prefix: str = "[ticket]") -> list[str]:
    """Static problems with a form. Called from scenario validation at build time."""
    if form is None:
        return []
    problems: list[str] = []
    if not form.fields:
        problems.append(f"{prefix} ticket.form declares no fields")
        return problems
    if not 0 < form.weight <= MAX_TICKET_WEIGHT:
        problems.append(
            f"{prefix} ticket.form.weight must be in (0, {MAX_TICKET_WEIGHT:g}] — the machine "
            "state is always the larger part of the grade"
        )
    if not 0 < form.pass_score <= 100:
        problems.append(f"{prefix} ticket.form.pass_score must be in (0, 100]")
    seen: set[str] = set()
    for fld in form.fields:
        if fld.id in RESERVED_FIELD_IDS:
            problems.append(
                f"{prefix} field id {fld.id!r} is reserved by the portal's write-up form "
                f"(reserved: {', '.join(sorted(RESERVED_FIELD_IDS))}); it would share the "
                "form control the submit buttons use"
            )
        if not re.fullmatch(r"[a-z0-9][a-z0-9_]*", fld.id or ""):
            problems.append(
                f"{prefix} field id {fld.id!r} must be lowercase, dash/underscore separated"
            )
        if fld.id in seen:
            problems.append(f"{prefix} duplicate field id {fld.id!r}")
        seen.add(fld.id)
        if not fld.label:
            problems.append(f"{prefix} field {fld.id!r} has no label")
        if fld.kind not in KINDS:
            problems.append(
                f"{prefix} field {fld.id!r} kind {fld.kind!r} must be one of: {', '.join(KINDS)}"
            )
        if fld.weight <= 0:
            problems.append(f"{prefix} field {fld.id!r} needs weight > 0")
        if fld.kind == "select" and not fld.options:
            problems.append(f"{prefix} select field {fld.id!r} needs options")
        if fld.min_words and fld.max_words and fld.min_words > fld.max_words:
            problems.append(f"{prefix} field {fld.id!r} min_words exceeds max_words")
        if fld.kind in {"text", "textarea"} and not (
            fld.min_words or fld.all_of or fld.any_of or fld.required
        ):
            problems.append(
                f"{prefix} free-text field {fld.id!r} has no rubric (min_words/keywords); "
                "it would score full marks for an empty-ish answer"
            )
    if form.fields and abs(form.total_weight - 100) > 0.01:
        problems.append(
            f"{prefix} ticket field weights total {form.total_weight:g}, not 100 "
            "(the ticket score is a weighted percentage)"
        )
    return problems


# --------------------------------------------------------------------------- #
# grading
# --------------------------------------------------------------------------- #


@dataclass
class TicketOutcome:
    field_id: str
    label: str
    passed: bool
    detail: str = ""
    weight: float = 0.0

    def to_dict(self) -> dict:
        return {
            "field_id": self.field_id,
            "label": self.label,
            "passed": self.passed,
            "detail": self.detail,
            "weight": self.weight,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TicketOutcome:
        return cls(
            field_id=str(data.get("field_id", "")),
            label=str(data.get("label", "")),
            passed=bool(data.get("passed", False)),
            detail=str(data.get("detail", "")),
            weight=float(data.get("weight", 0.0)),
        )


@dataclass
class TicketGrade:
    """The marked ticket."""

    session_id: int
    scenario_id: str
    score: float = 0.0
    outcomes: list[TicketOutcome] = field(default_factory=list)
    values: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    submitted: bool = False
    created_at: str = field(default_factory=iso)

    @property
    def passed_count(self) -> int:
        return sum(1 for o in self.outcomes if o.passed)

    def summary_line(self) -> str:
        if not self.submitted:
            return "no ticket submitted"
        return f"{self.score:.0f}% ({self.passed_count}/{len(self.outcomes)} fields)"

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "scenario_id": self.scenario_id,
            "score": self.score,
            "submitted": self.submitted,
            "created_at": self.created_at,
            "notes": list(self.notes),
            "values": dict(self.values),
            "outcomes": [o.to_dict() for o in self.outcomes],
        }

    @classmethod
    def from_dict(cls, data: dict) -> TicketGrade:
        return cls(
            session_id=int(data.get("session_id", 0)),
            scenario_id=str(data.get("scenario_id", "")),
            score=float(data.get("score", 0.0)),
            outcomes=[TicketOutcome.from_dict(o) for o in data.get("outcomes", [])],
            values={str(k): str(v) for k, v in (data.get("values") or {}).items()},
            notes=[str(n) for n in (data.get("notes") or [])],
            submitted=bool(data.get("submitted", False)),
            created_at=str(data.get("created_at", iso())),
        )


def word_count(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def _hits(haystack: str, terms: list[str]) -> tuple[list[str], list[str]]:
    """Return (found, missing) terms, case-insensitive substring match."""
    low = (haystack or "").lower()
    found = [t for t in terms if t.lower() in low]
    missing = [t for t in terms if t.lower() not in low]
    return found, missing


def grade_field(fld: TicketField, raw_value: Any) -> TicketOutcome:
    """Mark one field against its rubric."""
    value = "" if raw_value is None else str(raw_value).strip()
    outcome = TicketOutcome(
        field_id=fld.id, label=fld.label_or_id, passed=False, weight=fld.weight
    )

    if fld.is_choice():
        if not value:
            outcome.detail = "no option chosen"
            return outcome
        if fld.options and value not in fld.options:
            outcome.detail = f"unexpected option {value!r}"
            return outcome
        if fld.expected and value != fld.expected:
            outcome.detail = f"chose {value!r}; the correct classification is {fld.expected!r}"
            return outcome
        outcome.passed = True
        outcome.detail = f"chose {value!r}"
        return outcome

    if not value:
        outcome.detail = "left blank" if fld.required else "left blank (optional)"
        outcome.passed = not fld.required
        return outcome

    words = word_count(value)
    if fld.min_words and words < fld.min_words:
        outcome.detail = f"{words} word(s); a useful answer needs at least {fld.min_words}"
        return outcome
    if fld.max_words and words > fld.max_words:
        outcome.detail = f"{words} words; keep it under {fld.max_words}"
        return outcome

    found, missing = _hits(value, fld.all_of)
    if missing:
        outcome.detail = "never mentions " + ", ".join(missing)
        return outcome

    if fld.any_of:
        _, missing_any = _hits(value, fld.any_of)
        if len(missing_any) == len(fld.any_of):
            outcome.detail = "does not mention any of: " + ", ".join(fld.any_of)
            return outcome

    banned, _ = _hits(value, fld.none_of)
    if banned:
        outcome.detail = "contains a rejected answer: " + ", ".join(banned)
        return outcome

    outcome.passed = True
    outcome.detail = f"{words} word(s) recorded"
    return outcome


def grade(form: TicketForm | None, values: dict[str, Any], *, session_id: int = 0, scenario_id: str = "") -> TicketGrade:
    """Mark a whole submission. A missing form yields a zero-score, unsubmitted grade."""
    grade_out = TicketGrade(
        session_id=session_id,
        scenario_id=scenario_id,
        values={str(k): str(v) for k, v in (values or {}).items()},
    )
    if form is None:
        grade_out.notes.append("this scenario has no ticket form")
        return grade_out

    grade_out.submitted = any(str(v).strip() for v in (values or {}).values())
    for fld in form.fields:
        grade_out.outcomes.append(grade_field(fld, (values or {}).get(fld.id, "")))

    total = form.total_weight or 1.0
    earned = sum(o.weight for o in grade_out.outcomes if o.passed)
    grade_out.score = round(100.0 * earned / total, 1)
    if not grade_out.submitted:
        grade_out.notes.append("no ticket was submitted")
    return grade_out


def missing_required(form: TicketForm, values: dict[str, Any]) -> list[str]:
    """Labels of required fields still empty — used to block submission in the portal."""
    missing = []
    for fld in form.fields:
        if not fld.required:
            continue
        value = str((values or {}).get(fld.id, "") or "").strip()
        if not value:
            missing.append(fld.label_or_id)
    return missing


def render_feedback(form: TicketForm, grade_out: TicketGrade) -> list[dict]:
    """Per-field feedback rows for the portal and the CLI."""
    rows = []
    for outcome in grade_out.outcomes:
        fld = form.field(outcome.field_id)
        rows.append(
            {
                "field_id": outcome.field_id,
                "label": outcome.label,
                "passed": outcome.passed,
                "weight": outcome.weight,
                "detail": outcome.detail,
                "hint": fld.hint if fld and not outcome.passed else "",
            }
        )
    return rows


def feedback_text(form: TicketForm, grade_out: TicketGrade) -> str:
    lines = [
        f"Ticket: {form.title}",
        f"Score:  {grade_out.score:.1f}%  "
        f"({'submitted' if grade_out.submitted else 'not submitted'})",
        "",
    ]
    for row in render_feedback(form, grade_out):
        mark = "PASS" if row["passed"] else "FAIL"
        lines.append(f"  [{mark}] {row['label']} ({row['weight']:.0f} pts)")
        if row["detail"]:
            lines.append(f"         {row['detail']}")
    return "\n".join(lines)


def blend(machine_score: float, ticket: TicketGrade | None, weight: float) -> float:
    """Combine the machine grade and the ticket grade into the final mark.

    ``weight`` is the ticket's share of the final grade. No submission means the
    ticket contributes zero — which is the honest outcome: the work was not
    documented, so it does not count as done.
    """
    share = max(0.0, min(float(weight), MAX_TICKET_WEIGHT)) / 100.0
    ticket_score = ticket.score if ticket is not None and ticket.submitted else 0.0
    return round(machine_score * (1.0 - share) + ticket_score * share, 1)
