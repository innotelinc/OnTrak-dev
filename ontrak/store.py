"""SQLite persistence for users, sessions, grading results and events.

One file, no server. The load is a classroom (tens of concurrent users), so a
short-lived connection per call with WAL enabled is plenty and keeps the code
free of session/threading subtleties.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .auth import ACCOUNT_SENTINEL
from .models import ScoreReport, Session, SessionState, iso
from .tickets import TicketGrade

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL DEFAULT 'student',   -- student | instructor
    password_hash TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    student          TEXT NOT NULL,
    scenario_id      TEXT NOT NULL,
    state            TEXT NOT NULL,
    instance         TEXT NOT NULL DEFAULT '',
    host_ip          TEXT NOT NULL DEFAULT '',
    rdp_user         TEXT NOT NULL DEFAULT '',
    rdp_password     TEXT NOT NULL DEFAULT '',
    hint_level       INTEGER NOT NULL DEFAULT 0,
    checks_run       INTEGER NOT NULL DEFAULT 0,
    best_score       REAL NOT NULL DEFAULT 0,
    resolved         INTEGER NOT NULL DEFAULT 0,
    notes            TEXT NOT NULL DEFAULT '',
    error            TEXT NOT NULL DEFAULT '',
    workload         TEXT NOT NULL DEFAULT '',
    time_limit_minutes INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    ready_at         TEXT NOT NULL DEFAULT '',
    expires_at       TEXT NOT NULL DEFAULT '',
    last_activity_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_student ON sessions (student, state);
CREATE INDEX IF NOT EXISTS sessions_state   ON sessions (state);

CREATE TABLE IF NOT EXISTS results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL,
    student     TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    score       REAL NOT NULL,
    resolved    INTEGER NOT NULL DEFAULT 0,
    report_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS results_student ON results (student, scenario_id);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    kind       TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_kind ON events (kind);

-- The in-house ticket: what the student typed, and the marked version of it.
-- Kept as a separate table from results so a ticket can be read back (and shown
-- to the student) without unpacking a score report.
CREATE TABLE IF NOT EXISTS tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL,
    student     TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    values_json TEXT NOT NULL,
    report_json TEXT NOT NULL,
    score       REAL NOT NULL DEFAULT 0,
    submitted   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tickets_session ON tickets (session_id);
CREATE INDEX IF NOT EXISTS tickets_student ON tickets (student);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Columns the caller may update through update_session().
SESSION_COLUMNS = (
    "created_at",
    "state",
    "instance",
    "host_ip",
    "rdp_user",
    "rdp_password",
    "hint_level",
    "checks_run",
    "best_score",
    "resolved",
    "notes",
    "error",
    "workload",
    "time_limit_minutes",
    "ready_at",
    "expires_at",
    "last_activity_at",
)

# Columns added after the first release. init_schema() applies these to older
# databases rather than making the operator delete their state.
MIGRATIONS = (
    ("sessions", "workload", "TEXT NOT NULL DEFAULT ''"),
    ("sessions", "time_limit_minutes", "INTEGER NOT NULL DEFAULT 0"),
)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns introduced after a database was created.

        SQLite only supports ADD COLUMN for simple definitions, which is all these
        are; existing rows take the default, so an in-flight class keeps working
        across an upgrade.
        """
        for table, column, definition in MIGRATIONS:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------
    def upsert_user(self, username: str, role: str = "student", display_name: str = "") -> None:
        """Create an account row, or refresh the one that exists.

        There is no password: OnTrak is SSO-only, so every row — this one and the
        one a sign-in writes — carries `auth.ACCOUNT_SENTINEL` and nothing verifies a
        credential against it. This is for the accounts a range seeds itself (the
        demo roster), not for real identities: those arrive through Authentik and
        are created by `upsert_sso_user` on first sign-in.
        """
        username = username.strip().lower()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO users (username, display_name, role, password_hash, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    password_hash=excluded.password_hash,
                    role=excluded.role,
                    display_name=excluded.display_name,
                    active=1
                """,
                (username, display_name or username, role, ACCOUNT_SENTINEL, iso()),
            )

    def upsert_sso_user(
        self, username: str, display_name: str = "", role: str = "student"
    ) -> None:
        """Create the row an Authentik sign-in needs, or refresh the one it has.

        `active` is deliberately NOT touched on an existing row: deactivating an
        account here is the range's own control — an instructor taking a student
        off the board — and an SSO sign-in must not quietly undo it. The password
        column gets `auth.ACCOUNT_SENTINEL`, the same sentinel every row carries, so
        nothing here can ever verify as a credential.
        """
        username = username.strip().lower()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO users (username, display_name, role, password_hash, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    role=excluded.role,
                    display_name=excluded.display_name
                """,
                (username, display_name or username, role, ACCOUNT_SENTINEL, iso()),
            )

    def get_user(self, username: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM users WHERE username = ? AND active = 1", (username.strip().lower(),)
            ).fetchone()

    def list_users(self, role: str | None = None) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if role:
                return list(
                    conn.execute(
                        "SELECT * FROM users WHERE role = ? AND active = 1 ORDER BY username", (role,)
                    )
                )
            return list(conn.execute("SELECT * FROM users WHERE active = 1 ORDER BY username"))

    def deactivate_user(self, username: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET active = 0 WHERE username = ?", (username.strip().lower(),)
            )

    def activate_user(self, username: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET active = 1 WHERE username = ?", (username.strip().lower(),)
            )

    def delete_user(self, username: str) -> None:
        """Remove an account row outright.

        Offboarding a *portal* account is not the same as offboarding a person:
        their results and tickets are kept, because a marking record that the
        instructor can delete is not a marking record. Only the row here goes —
        and because identity is Authentik's, the person can still sign in, which
        creates a fresh row. Revoke the person in Authentik, not here.
        """
        with self.connect() as conn:
            conn.execute("DELETE FROM users WHERE username = ?", (username.strip().lower(),))

    def list_all_users(self) -> list[sqlite3.Row]:
        """Every account, active or not — the admin panel's user list."""
        with self.connect() as conn:
            return list(conn.execute("SELECT * FROM users ORDER BY role, username"))

    def set_user_role(self, username: str, role: str) -> None:
        if role not in {"student", "instructor"}:
            raise ValueError(f"unknown role {role!r}")
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET role = ? WHERE username = ?", (role, username.strip().lower())
            )

    def count_users(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT role, active, COUNT(*) AS n FROM users GROUP BY role, active"
            ).fetchall()
        out = {"student": 0, "instructor": 0, "inactive": 0}
        for row in rows:
            if not row["active"]:
                out["inactive"] += int(row["n"])
            elif row["role"] in out:
                out[row["role"]] += int(row["n"])
        return out

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------
    def create_session(self, session: Session) -> Session:
        payload = session.to_dict()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions (
                    student, scenario_id, state, instance, host_ip, rdp_user, rdp_password,
                    hint_level, checks_run, best_score, resolved, notes, error, workload,
                    time_limit_minutes, created_at, ready_at, expires_at, last_activity_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    session.student,
                    session.scenario_id,
                    session.state.value,
                    session.instance,
                    session.host_ip,
                    session.rdp_user,
                    session.rdp_password,
                    session.hint_level,
                    session.checks_run,
                    session.best_score,
                    int(session.resolved),
                    session.notes,
                    session.error,
                    session.workload,
                    session.time_limit_minutes,
                    payload["created_at"],
                    session.ready_at,
                    session.expires_at,
                    payload["last_activity_at"],
                ),
            )
            session.id = int(cur.lastrowid or 0)
        return session

    def get_session(self, session_id: int) -> Session | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return self._row_to_session(row) if row else None

    def update_session(self, session_id: int, **fields: Any) -> None:
        unknown = set(fields) - set(SESSION_COLUMNS)
        if unknown:
            raise ValueError(f"cannot update session column(s): {', '.join(sorted(unknown))}")
        if not fields:
            return
        values = []
        for key, value in fields.items():
            if key == "state" and isinstance(value, SessionState):
                value = value.value
            if key == "resolved":
                value = int(bool(value))
            values.append(value)
        clause = ", ".join(f"{key} = ?" for key in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE sessions SET {clause} WHERE id = ?", (*values, session_id))

    def save_session(self, session: Session) -> None:
        if session.id is None:
            raise ValueError("session has no id; use create_session()")
        self.update_session(
            session.id,
            state=session.state,
            instance=session.instance,
            host_ip=session.host_ip,
            rdp_user=session.rdp_user,
            rdp_password=session.rdp_password,
            hint_level=session.hint_level,
            checks_run=session.checks_run,
            best_score=session.best_score,
            resolved=session.resolved,
            notes=session.notes,
            error=session.error,
            workload=session.workload,
            time_limit_minutes=session.time_limit_minutes,
            ready_at=session.ready_at,
            expires_at=session.expires_at,
            last_activity_at=session.last_activity_at,
        )

    def list_sessions(
        self,
        states: Sequence[SessionState] | None = None,
        student: str | None = None,
        scenario_id: str | None = None,
        limit: int = 200,
    ) -> list[Session]:
        where, params = [], []
        if states:
            placeholders = ",".join("?" for _ in states)
            where.append(f"state IN ({placeholders})")
            params.extend(s.value for s in states)
        if student:
            where.append("student = ?")
            params.append(student.strip().lower())
        if scenario_id:
            where.append("scenario_id = ?")
            params.append(scenario_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM sessions {clause} ORDER BY id DESC LIMIT ?", params
            ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def live_sessions_for(self, student: str) -> list[Session]:
        return [
            s
            for s in self.list_sessions(student=student)
            if s.state.is_live
        ]

    def count_sessions(self, states: Sequence[SessionState] | None = None) -> int:
        params: list[Any] = []
        clause = ""
        if states:
            placeholders = ",".join("?" for _ in states)
            clause = f"WHERE state IN ({placeholders})"
            params.extend(s.value for s in states)
        with self.connect() as conn:
            return int(
                conn.execute(f"SELECT COUNT(*) AS n FROM sessions {clause}", params).fetchone()["n"]
            )

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> Session:
        data = dict(row)
        try:
            state = SessionState(data["state"])
        except ValueError:
            state = SessionState.ERROR
        return Session(
            id=data["id"],
            student=data["student"],
            scenario_id=data["scenario_id"],
            state=state,
            instance=data["instance"],
            host_ip=data["host_ip"],
            rdp_user=data["rdp_user"],
            rdp_password=data["rdp_password"],
            hint_level=data["hint_level"],
            checks_run=data["checks_run"],
            best_score=data["best_score"],
            resolved=bool(data["resolved"]),
            notes=data["notes"],
            error=data["error"],
            workload=data.get("workload", "") or "",
            time_limit_minutes=data.get("time_limit_minutes", 0) or 0,
            created_at=data["created_at"],
            ready_at=data["ready_at"],
            expires_at=data["expires_at"],
            last_activity_at=data["last_activity_at"],
        )

    # ------------------------------------------------------------------
    # results
    # ------------------------------------------------------------------
    def add_result(self, report: ScoreReport, student: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO results (session_id, student, scenario_id, score, resolved,
                                     report_json, created_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    report.session_id,
                    student.strip().lower(),
                    report.scenario_id,
                    report.score,
                    int(report.resolved),
                    json.dumps(report.to_dict()),
                    report.created_at,
                ),
            )

    def latest_report(self, session_id: int) -> ScoreReport | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT report_json FROM results WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return ScoreReport.from_dict(json.loads(row["report_json"])) if row else None

    def results_for_student(self, student: str) -> list[ScoreReport]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT report_json FROM results WHERE student = ? ORDER BY id",
                (student.strip().lower(),),
            ).fetchall()
        return [ScoreReport.from_dict(json.loads(r["report_json"])) for r in rows]

    def leaderboard(self) -> list[dict[str, Any]]:
        """Best score per (student, scenario)."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT student, scenario_id, MAX(score) AS best, MAX(resolved) AS solved,
                       COUNT(*) AS attempts
                FROM results GROUP BY student, scenario_id
                ORDER BY student, scenario_id
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def attempt_counts(self, session_id: int) -> int:
        with self.connect() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM results WHERE session_id = ?", (session_id,)
                ).fetchone()["n"]
            )

    def list_result_rows(self, limit: int = 200, student: str | None = None) -> list[dict[str, Any]]:
        """Stored submissions with the student who made them.

        The student is a column on ``results`` rather than a field on the report, so
        an export needs both: fetching only the report would lose who submitted it.
        """
        where, params = "", []
        if student:
            where = "WHERE student = ?"
            params.append(student.strip().lower())
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT student, report_json, created_at FROM results {where} "
                "ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return [
            {
                "student": str(row["student"]),
                "submitted_at": str(row["created_at"]),
                "report": ScoreReport.from_dict(json.loads(row["report_json"])),
            }
            for row in rows
        ]

    def list_results(self, limit: int = 200, student: str | None = None) -> list[ScoreReport]:
        """Every stored submission, newest first (the admin results view)."""
        where, params = "", []
        if student:
            where = "WHERE student = ?"
            params.append(student.strip().lower())
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT report_json FROM results {where} ORDER BY id DESC LIMIT ?", params
            ).fetchall()
        return [ScoreReport.from_dict(json.loads(r["report_json"])) for r in rows]

    # ------------------------------------------------------------------
    # tickets (the in-house incident write-up)
    # ------------------------------------------------------------------
    def save_ticket(self, grade: TicketGrade, student: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO tickets (session_id, student, scenario_id, values_json,
                                     report_json, score, submitted, created_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    grade.session_id,
                    student.strip().lower(),
                    grade.scenario_id,
                    json.dumps(grade.values),
                    json.dumps(grade.to_dict()),
                    grade.score,
                    int(grade.submitted),
                    grade.created_at,
                ),
            )

    def latest_ticket(self, session_id: int) -> TicketGrade | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT report_json FROM tickets WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return TicketGrade.from_dict(json.loads(row["report_json"])) if row else None

    def ticket_values(self, session_id: int) -> dict[str, str]:
        """The raw answers last submitted for a session (to re-populate the form)."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT values_json FROM tickets WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if row is None:
            return {}
        try:
            loaded = json.loads(row["values_json"])
        except json.JSONDecodeError:
            return {}
        return {str(k): str(v) for k, v in (loaded or {}).items()} if isinstance(loaded, dict) else {}

    def save_ticket_draft(self, session_id: int, values: dict[str, Any]) -> None:
        """Remember what the student has typed so far.

        Drafts live in ``meta`` on purpose: the ``tickets`` table means "a graded
        submission", and the lab is results-only — an unsubmitted draft is not a
        grade and must not appear in a report.
        """
        self.set_meta(f"ticket_draft:{int(session_id)}", {str(k): str(v) for k, v in (values or {}).items()})

    def ticket_draft(self, session_id: int) -> dict[str, str]:
        draft = self.get_meta(f"ticket_draft:{int(session_id)}", {}) or {}
        return {str(k): str(v) for k, v in draft.items()} if isinstance(draft, dict) else {}

    def clear_ticket_draft(self, session_id: int) -> None:
        self.set_meta(f"ticket_draft:{int(session_id)}", {})

    def tickets_for_session(self, session_id: int) -> list[TicketGrade]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT report_json FROM tickets WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall()
        return [TicketGrade.from_dict(json.loads(r["report_json"])) for r in rows]

    def tickets_for_student(self, student: str, limit: int = 50) -> list[TicketGrade]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT report_json FROM tickets WHERE student = ? ORDER BY id DESC LIMIT ?",
                (student.strip().lower(), limit),
            ).fetchall()
        return [TicketGrade.from_dict(json.loads(r["report_json"])) for r in rows]

    def list_tickets(self, limit: int = 200) -> list[dict[str, Any]]:
        """Ticket rows plus the student who wrote them (the admin ticket view)."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, student, scenario_id, score, submitted, created_at
                FROM tickets ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_tickets(self) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, AVG(score) AS avg, SUM(submitted) AS submitted FROM tickets"
            ).fetchone()
        return {
            "count": int(row["n"] or 0),
            "average": round(float(row["avg"] or 0.0), 1),
            "submitted": int(row["submitted"] or 0),
        }

    # ------------------------------------------------------------------
    # events / meta
    # ------------------------------------------------------------------
    def log_event(self, kind: str, detail: str = "", session_id: int | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO events (session_id, kind, detail, created_at) VALUES (?,?,?,?)",
                (session_id, kind, detail, iso()),
            )

    def events_for(self, session_id: int, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM events WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                    (session_id, limit),
                )
            )

    def recent_events(self, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)))

    def list_events(
        self, kind: str | None = None, limit: int = 200, session_id: int | None = None
    ) -> list[sqlite3.Row]:
        """The audit trail, filtered. Everything the control plane does lands here."""
        where, params = [], []
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"SELECT * FROM events {clause} ORDER BY id DESC LIMIT ?", params
                )
            )

    def event_kinds(self) -> list[tuple[str, int]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT kind, COUNT(*) AS n FROM events GROUP BY kind ORDER BY n DESC, kind"
            ).fetchall()
        return [(str(r["kind"]), int(r["n"])) for r in rows]

    def count_events(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"])

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def set_meta(self, key: str, value: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
