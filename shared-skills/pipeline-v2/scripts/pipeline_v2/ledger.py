"""SQLite ledger for pipeline v2 batches, cases, and hits."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .schema import CASE_STATUSES, ORACLE_LEVELS, SENTINEL_STATUSES, TRIAGE_RESULTS


def now_iso() -> str:
    """Return an ISO-8601 UTC timestamp."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


class Ledger:
    """Thin SQLite access layer with schema-level safety checks."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        oracle_values = ",".join(repr(value) for value in sorted(ORACLE_LEVELS))
        status_values = ",".join(repr(value) for value in sorted(CASE_STATUSES))
        triage_values = ",".join(repr(value) for value in sorted(TRIAGE_RESULTS))
        sentinel_values = ",".join(repr(value) for value in sorted(SENTINEL_STATUSES))
        self.conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS cases (
              fingerprint TEXT PRIMARY KEY,
              status TEXT NOT NULL CHECK (status IN ({status_values})),
              oracle_level TEXT NOT NULL CHECK (oracle_level IN ({oracle_values})),
              llm_involvement TEXT NOT NULL CHECK (length(trim(llm_involvement)) > 0),
              semantic_map_entry_id TEXT,
              first_seen_batch TEXT NOT NULL,
              last_seen_batch TEXT NOT NULL,
              hit_count INTEGER NOT NULL DEFAULT 0 CHECK (hit_count >= 0),
              verify_history TEXT NOT NULL DEFAULT '[]',
              triage_result TEXT CHECK (triage_result IS NULL OR triage_result IN ({triage_values})),
              report_ref TEXT,
              fix_ref TEXT,
              updated_at TEXT NOT NULL,
              CHECK (
                oracle_level <> 'L2'
                OR (semantic_map_entry_id IS NOT NULL AND length(trim(semantic_map_entry_id)) > 0)
              )
            );

            CREATE TABLE IF NOT EXISTS batches (
              batch_id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              product_version TEXT,
              environment_digest TEXT,
              signal_count INTEGER NOT NULL DEFAULT 0,
              sentinel_blocked_count INTEGER NOT NULL DEFAULT 0,
              oracle_l1_count INTEGER NOT NULL DEFAULT 0,
              oracle_l2_count INTEGER NOT NULL DEFAULT 0,
              oracle_l3_count INTEGER NOT NULL DEFAULT 0,
              verification_passed_count INTEGER NOT NULL DEFAULT 0,
              verification_blocked_count INTEGER NOT NULL DEFAULT 0,
              freshness_mismatch_count INTEGER NOT NULL DEFAULT 0,
              llm_vetoed_count INTEGER NOT NULL DEFAULT 0,
              reported_count INTEGER NOT NULL DEFAULT 0,
              observation_entered_count INTEGER NOT NULL DEFAULT 0,
              observation_upgraded_count INTEGER NOT NULL DEFAULT 0,
              observation_expired_count INTEGER NOT NULL DEFAULT 0,
              token_cost TEXT NOT NULL DEFAULT '{{}}',
              duration TEXT NOT NULL DEFAULT '{{}}',
              sentinel_status TEXT NOT NULL DEFAULT 'skipped'
                CHECK (sentinel_status IN ({sentinel_values})),
              redline_flags TEXT NOT NULL DEFAULT '[]',
              injected_recall_rate REAL
            );

            CREATE TABLE IF NOT EXISTS hits (
              fingerprint TEXT NOT NULL,
              batch_id TEXT NOT NULL,
              failure_summary TEXT NOT NULL,
              observed_at TEXT NOT NULL,
              PRIMARY KEY (fingerprint, batch_id),
              FOREIGN KEY (fingerprint) REFERENCES cases(fingerprint)
                ON UPDATE CASCADE ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS model_calls (
              call_id TEXT PRIMARY KEY,
              batch_id TEXT NOT NULL,
              fingerprint TEXT,
              role TEXT NOT NULL CHECK (role IN ('cheap', 'standard', 'strong')),
              model_id TEXT NOT NULL,
              prompt_version TEXT NOT NULL,
              trigger TEXT NOT NULL,
              input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
              output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
              estimated_cost REAL NOT NULL DEFAULT 0 CHECK (estimated_cost >= 0),
              duration_ms INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
              validation_result TEXT NOT NULL,
              escalated_from TEXT NOT NULL,
              decision TEXT NOT NULL CHECK (decision IN ('allow', 'veto', 'request-more-evidence')),
              created_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def upsert_case(
        self,
        *,
        fingerprint: str,
        status: str,
        oracle_level: str,
        llm_involvement: str,
        batch_id: str,
        verify_entry: Optional[Dict[str, Any]] = None,
        triage_result: Optional[str] = None,
        report_ref: Optional[str] = None,
        fix_ref: Optional[str] = None,
        semantic_map_entry_id: Optional[str] = None,
    ) -> None:
        row = self.get_case(fingerprint)
        ts = now_iso()
        if row is None:
            history: List[Dict[str, Any]] = []
            if verify_entry is not None:
                history.append(verify_entry)
            self.conn.execute(
                """
                INSERT INTO cases (
                  fingerprint, status, oracle_level, llm_involvement,
                  semantic_map_entry_id, first_seen_batch, last_seen_batch,
                  hit_count, verify_history, triage_result, report_ref, fix_ref,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    fingerprint,
                    status,
                    oracle_level,
                    llm_involvement,
                    semantic_map_entry_id,
                    batch_id,
                    batch_id,
                    _json(history),
                    triage_result,
                    report_ref,
                    fix_ref,
                    ts,
                ),
            )
        else:
            history = json.loads(row["verify_history"] or "[]")
            if verify_entry is not None:
                history.append(verify_entry)
            self.conn.execute(
                """
                UPDATE cases
                   SET status = ?,
                       oracle_level = ?,
                       llm_involvement = ?,
                       semantic_map_entry_id = COALESCE(?, semantic_map_entry_id),
                       last_seen_batch = ?,
                       hit_count = hit_count + 1,
                       verify_history = ?,
                       triage_result = COALESCE(?, triage_result),
                       report_ref = COALESCE(?, report_ref),
                       fix_ref = COALESCE(?, fix_ref),
                       updated_at = ?
                 WHERE fingerprint = ?
                """,
                (
                    status,
                    oracle_level,
                    llm_involvement,
                    semantic_map_entry_id,
                    batch_id,
                    _json(history),
                    triage_result,
                    report_ref,
                    fix_ref,
                    ts,
                    fingerprint,
                ),
            )
        self.conn.commit()

    def update_case_refs(
        self,
        fingerprint: str,
        *,
        report_ref: Optional[str] = None,
        fix_ref: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE cases
               SET report_ref = COALESCE(?, report_ref),
                   fix_ref = COALESCE(?, fix_ref),
                   updated_at = ?
             WHERE fingerprint = ?
            """,
            (report_ref, fix_ref, now_iso(), fingerprint),
        )
        self.conn.commit()

    def set_case_status(self, fingerprint: str, status: str) -> None:
        self.conn.execute(
            "UPDATE cases SET status = ?, updated_at = ? WHERE fingerprint = ?",
            (status, now_iso(), fingerprint),
        )
        self.conn.commit()

    def update_triage_result(self, fingerprint: str, triage_result: str) -> None:
        self.conn.execute(
            "UPDATE cases SET triage_result = ?, updated_at = ? WHERE fingerprint = ?",
            (triage_result, now_iso(), fingerprint),
        )
        self.conn.commit()

    def upsert_batch(
        self,
        *,
        batch_id: str,
        product_version: str = "",
        environment_digest: str = "",
        signal_count: int = 0,
        sentinel_blocked_count: int = 0,
        oracle_l1_count: int = 0,
        oracle_l2_count: int = 0,
        oracle_l3_count: int = 0,
        verification_passed_count: int = 0,
        verification_blocked_count: int = 0,
        freshness_mismatch_count: int = 0,
        llm_vetoed_count: int = 0,
        reported_count: int = 0,
        observation_entered_count: int = 0,
        observation_upgraded_count: int = 0,
        observation_expired_count: int = 0,
        token_cost: Optional[Dict[str, Any]] = None,
        duration: Optional[Dict[str, Any]] = None,
        sentinel_status: str = "skipped",
        redline_flags: Optional[List[str]] = None,
        injected_recall_rate: Optional[float] = None,
    ) -> None:
        existing = self.get_batch(batch_id)
        created_at = existing["created_at"] if existing else now_iso()
        self.conn.execute(
            """
            INSERT INTO batches (
              batch_id, created_at, product_version, environment_digest,
              signal_count, sentinel_blocked_count, oracle_l1_count,
              oracle_l2_count, oracle_l3_count, verification_passed_count,
              verification_blocked_count, freshness_mismatch_count,
              llm_vetoed_count, reported_count, observation_entered_count,
              observation_upgraded_count, observation_expired_count,
              token_cost, duration, sentinel_status, redline_flags,
              injected_recall_rate
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(batch_id) DO UPDATE SET
              product_version = excluded.product_version,
              environment_digest = excluded.environment_digest,
              signal_count = excluded.signal_count,
              sentinel_blocked_count = excluded.sentinel_blocked_count,
              oracle_l1_count = excluded.oracle_l1_count,
              oracle_l2_count = excluded.oracle_l2_count,
              oracle_l3_count = excluded.oracle_l3_count,
              verification_passed_count = excluded.verification_passed_count,
              verification_blocked_count = excluded.verification_blocked_count,
              freshness_mismatch_count = excluded.freshness_mismatch_count,
              llm_vetoed_count = excluded.llm_vetoed_count,
              reported_count = excluded.reported_count,
              observation_entered_count = excluded.observation_entered_count,
              observation_upgraded_count = excluded.observation_upgraded_count,
              observation_expired_count = excluded.observation_expired_count,
              token_cost = excluded.token_cost,
              duration = excluded.duration,
              sentinel_status = excluded.sentinel_status,
              redline_flags = excluded.redline_flags,
              injected_recall_rate = excluded.injected_recall_rate
            """,
            (
                batch_id,
                created_at,
                product_version,
                environment_digest,
                signal_count,
                sentinel_blocked_count,
                oracle_l1_count,
                oracle_l2_count,
                oracle_l3_count,
                verification_passed_count,
                verification_blocked_count,
                freshness_mismatch_count,
                llm_vetoed_count,
                reported_count,
                observation_entered_count,
                observation_upgraded_count,
                observation_expired_count,
                _json(token_cost or {}),
                _json(duration or {}),
                sentinel_status,
                _json(redline_flags or []),
                injected_recall_rate,
            ),
        )
        self.conn.commit()

    def update_redline_flags(self, batch_id: str, flags: List[str]) -> None:
        self.conn.execute(
            "UPDATE batches SET redline_flags = ? WHERE batch_id = ?",
            (_json(flags), batch_id),
        )
        self.conn.commit()

    def record_hit(self, *, fingerprint: str, batch_id: str, failure_summary: str) -> None:
        self.conn.execute(
            """
            INSERT INTO hits (fingerprint, batch_id, failure_summary, observed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(fingerprint, batch_id) DO UPDATE SET
              failure_summary = excluded.failure_summary,
              observed_at = excluded.observed_at
            """,
            (fingerprint, batch_id, failure_summary, now_iso()),
        )
        self.conn.commit()

    def get_case(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM cases WHERE fingerprint = ?", (fingerprint,)).fetchone()
        return dict(row) if row else None

    def record_model_call(
        self,
        *,
        call_id: str,
        batch_id: str,
        fingerprint: Optional[str],
        role: str,
        model_id: str,
        prompt_version: str,
        trigger: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost: float,
        duration_ms: int,
        validation_result: str,
        escalated_from: str,
        decision: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO model_calls (
              call_id, batch_id, fingerprint, role, model_id, prompt_version,
              trigger, input_tokens, output_tokens, estimated_cost, duration_ms,
              validation_result, escalated_from, decision, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                call_id,
                batch_id,
                fingerprint,
                role,
                model_id,
                prompt_version,
                trigger,
                input_tokens,
                output_tokens,
                estimated_cost,
                duration_ms,
                validation_result,
                escalated_from,
                decision,
                now_iso(),
            ),
        )
        self.conn.commit()

    def list_model_calls_for_batch(self, batch_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM model_calls WHERE batch_id = ? ORDER BY created_at, call_id",
            (batch_id,),
        )
        return [dict(row) for row in rows.fetchall()]

    def update_batch_metrics(
        self,
        batch_id: str,
        *,
        token_cost: Dict[str, Any],
        duration: Dict[str, Any],
    ) -> None:
        row = self.get_batch(batch_id)
        existing_token_cost = json.loads(row["token_cost"] or "{}") if row else {}
        existing_duration = json.loads(row["duration"] or "{}") if row else {}
        existing_token_cost.update(token_cost)
        existing_duration.update(duration)
        self.conn.execute(
            "UPDATE batches SET token_cost = ?, duration = ? WHERE batch_id = ?",
            (_json(existing_token_cost), _json(existing_duration), batch_id),
        )
        self.conn.commit()

    def get_batch(self, batch_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
        return dict(row) if row else None

    def list_cases(self, *, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            rows = self.conn.execute("SELECT * FROM cases WHERE status = ? ORDER BY fingerprint", (status,))
        else:
            rows = self.conn.execute("SELECT * FROM cases ORDER BY fingerprint")
        return [dict(row) for row in rows.fetchall()]

    def list_reported_cases_for_batch(self, batch_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT cases.*
              FROM cases
              JOIN hits ON hits.fingerprint = cases.fingerprint
             WHERE hits.batch_id = ?
               AND cases.status IN ('reported', 'intermittent')
             ORDER BY cases.fingerprint
            """,
            (batch_id,),
        )
        return [dict(row) for row in rows.fetchall()]

    def list_hits_for_batch(self, batch_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM hits WHERE batch_id = ? ORDER BY fingerprint", (batch_id,))
        return [dict(row) for row in rows.fetchall()]

    def distinct_hit_batches(self, fingerprint: str) -> List[str]:
        rows = self.conn.execute(
            "SELECT batch_id FROM hits WHERE fingerprint = ? ORDER BY batch_id",
            (fingerprint,),
        )
        return [row["batch_id"] for row in rows.fetchall()]

    def recent_batches(self, limit: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM batches ORDER BY created_at DESC, batch_id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows.fetchall()]

    def all_batches(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM batches ORDER BY created_at ASC, batch_id ASC")
        return [dict(row) for row in rows.fetchall()]

    def count_observing_cases(self) -> int:
        row = self.conn.execute("SELECT count(*) AS n FROM cases WHERE status = 'observing'").fetchone()
        return int(row["n"])

    def cases_with_report_refs(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM cases
             WHERE report_ref IS NOT NULL
               AND status IN ('reported', 'intermittent')
            """
        )
        return [dict(row) for row in rows.fetchall()]
