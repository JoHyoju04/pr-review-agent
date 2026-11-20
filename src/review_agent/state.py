"""라운드 상태와 피드백 로그 저장소 (SQLite 파일 하나)."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from .models import Finding, PostedRef, Verdict

REACTIONS: tuple[str, ...] = ("fixed", "wrong", "intentional", "ignored")

DEFAULT_STATE_DIR = ".review-agent"
DEFAULT_STATE_FILENAME = "state.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pr_state (
    repo_key          TEXT    NOT NULL,
    pr_number         INTEGER NOT NULL,
    rounds            INTEGER NOT NULL DEFAULT 0,
    last_reviewed_sha TEXT    NOT NULL DEFAULT '',
    stopped           INTEGER NOT NULL DEFAULT 0,
    rounds_to_pass    INTEGER,
    updated_at        TEXT    NOT NULL,
    PRIMARY KEY (repo_key, pr_number)
);

CREATE TABLE IF NOT EXISTS posted_findings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key     TEXT    NOT NULL,
    pr_number    INTEGER NOT NULL,
    round        INTEGER NOT NULL,
    finding_id   TEXT    NOT NULL,
    fingerprint  TEXT    NOT NULL,
    file         TEXT    NOT NULL,
    line_start   INTEGER NOT NULL,
    line_end     INTEGER NOT NULL,
    category     TEXT    NOT NULL,
    severity     TEXT    NOT NULL,
    claim        TEXT    NOT NULL,
    detected_by  TEXT    NOT NULL,
    finding_json TEXT    NOT NULL,
    votes_json   TEXT    NOT NULL,
    posted_at    TEXT    NOT NULL,
    model        TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_posted_pr ON posted_findings (repo_key, pr_number);

CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    posted_id  INTEGER NOT NULL REFERENCES posted_findings (id),
    reaction   TEXT    NOT NULL,
    note       TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL
);
"""


def default_state_path() -> str:
    env = os.environ.get("REVIEW_AGENT_STATE")
    if env:
        return env
    return os.path.join(os.getcwd(), DEFAULT_STATE_DIR, DEFAULT_STATE_FILENAME)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class PRState:
    repo_key: str
    pr_number: int
    rounds: int = 0
    last_reviewed_sha: str = ""
    stopped: bool = False
    rounds_to_pass: Optional[int] = None

    @property
    def next_round(self) -> int:
        return self.rounds + 1


class StateStore:
    """PR별 라운드, 마지막 리뷰 SHA, 게시한 지문, 피드백 로그."""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """예전 DB에 없는 컬럼을 채운다. CREATE TABLE IF NOT EXISTS만으로는 안 생긴다."""
        existing = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(posted_findings)")
        }
        for column, ddl in (("model", "TEXT NOT NULL DEFAULT ''"),):
            if column not in existing:
                self.conn.execute(
                    f"ALTER TABLE posted_findings ADD COLUMN {column} {ddl}"
                )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- 라운드 상태 ----

    def get_state(self, repo_key: str, pr_number: int) -> PRState:
        row = self.conn.execute(
            "SELECT * FROM pr_state WHERE repo_key = ? AND pr_number = ?",
            (repo_key, pr_number),
        ).fetchone()
        if row is None:
            return PRState(repo_key=repo_key, pr_number=pr_number)
        return PRState(
            repo_key=repo_key,
            pr_number=pr_number,
            rounds=row["rounds"],
            last_reviewed_sha=row["last_reviewed_sha"],
            stopped=bool(row["stopped"]),
            rounds_to_pass=row["rounds_to_pass"],
        )

    def save_state(self, state: PRState) -> None:
        self.conn.execute(
            """
            INSERT INTO pr_state
                (repo_key, pr_number, rounds, last_reviewed_sha, stopped, rounds_to_pass, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (repo_key, pr_number) DO UPDATE SET
                rounds = excluded.rounds,
                last_reviewed_sha = excluded.last_reviewed_sha,
                stopped = excluded.stopped,
                rounds_to_pass = excluded.rounds_to_pass,
                updated_at = excluded.updated_at
            """,
            (
                state.repo_key,
                state.pr_number,
                state.rounds,
                state.last_reviewed_sha,
                1 if state.stopped else 0,
                state.rounds_to_pass,
                _now(),
            ),
        )
        self.conn.commit()

    # ---- 게시한 finding ----

    def posted_refs(self, repo_key: str, pr_number: int) -> list[PostedRef]:
        rows = self.conn.execute(
            """
            SELECT finding_id, fingerprint, file, line_start, line_end, category, claim, round
            FROM posted_findings
            WHERE repo_key = ? AND pr_number = ?
            ORDER BY id
            """,
            (repo_key, pr_number),
        ).fetchall()
        return [
            PostedRef(
                file=row["file"],
                line_start=row["line_start"],
                line_end=row["line_end"],
                category=row["category"],
                fingerprint=row["fingerprint"],
                claim=row["claim"],
                finding_id=row["finding_id"],
                round=row["round"],
            )
            for row in rows
        ]

    def record_posted(
        self,
        repo_key: str,
        pr_number: int,
        round_number: int,
        findings: Sequence[Finding],
        verdicts_by_finding: Optional[dict[str, Sequence[Verdict]]] = None,
        model: str = "",
    ) -> list[int]:
        """게시한 finding과 검증자별 투표 결과를 피드백 로그용으로 남긴다.

        `model`을 함께 기록한다. 모델이 바뀌면 결과가 달라지므로 나중에 비교할 때 필요하다.
        """
        verdicts_by_finding = verdicts_by_finding or {}
        ids: list[int] = []
        for finding in findings:
            votes = [v.to_dict() for v in verdicts_by_finding.get(finding.id, [])]
            cursor = self.conn.execute(
                """
                INSERT INTO posted_findings
                    (repo_key, pr_number, round, finding_id, fingerprint, file, line_start,
                     line_end, category, severity, claim, detected_by, finding_json, votes_json,
                     posted_at, model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo_key,
                    pr_number,
                    round_number,
                    finding.id,
                    finding.fingerprint,
                    finding.file,
                    finding.line_start,
                    finding.line_end,
                    finding.category,
                    finding.severity,
                    finding.claim,
                    json.dumps(finding.detected_by, ensure_ascii=False),
                    json.dumps(finding.to_dict(), ensure_ascii=False),
                    json.dumps(votes, ensure_ascii=False),
                    _now(),
                    model,
                ),
            )
            ids.append(int(cursor.lastrowid or 0))
        self.conn.commit()
        return ids

    def list_posted(self, repo_key: str, pr_number: int) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT p.*, f.reaction, f.note, f.created_at AS reaction_at
            FROM posted_findings AS p
            LEFT JOIN feedback AS f ON f.posted_id = p.id
            WHERE p.repo_key = ? AND p.pr_number = ?
            ORDER BY p.round, p.id
            """,
            (repo_key, pr_number),
        ).fetchall()
        return [dict(row) for row in rows]

    # ---- 피드백 로그 ----

    def record_feedback(
        self,
        repo_key: str,
        pr_number: int,
        finding_id: str,
        reaction: str,
        note: str = "",
        round_number: Optional[int] = None,
    ) -> bool:
        """작성자 반응을 기록한다. 대상 finding을 못 찾으면 False."""
        if reaction not in REACTIONS:
            raise ValueError(f"reaction은 {', '.join(REACTIONS)} 중 하나여야 합니다: {reaction!r}")

        query = """
            SELECT id FROM posted_findings
            WHERE repo_key = ? AND pr_number = ? AND finding_id = ?
        """
        params: list[object] = [repo_key, pr_number, finding_id]
        if round_number is not None:
            query += " AND round = ?"
            params.append(round_number)
        query += " ORDER BY round DESC, id DESC LIMIT 1"

        row = self.conn.execute(query, params).fetchone()
        if row is None:
            return False

        self.conn.execute(
            "INSERT INTO feedback (posted_id, reaction, note, created_at) VALUES (?, ?, ?, ?)",
            (row["id"], reaction, note, _now()),
        )
        self.conn.commit()
        return True

    def feedback_summary(self, repo_key: str, pr_number: Optional[int] = None) -> dict[str, int]:
        query = """
            SELECT f.reaction, COUNT(*) AS n
            FROM feedback AS f
            JOIN posted_findings AS p ON p.id = f.posted_id
            WHERE p.repo_key = ?
        """
        params: list[object] = [repo_key]
        if pr_number is not None:
            query += " AND p.pr_number = ?"
            params.append(pr_number)
        query += " GROUP BY f.reaction"
        return {row["reaction"]: row["n"] for row in self.conn.execute(query, params)}
