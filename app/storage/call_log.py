"""Persistent call log backed by SQLite (stdlib — no extra infrastructure).

Every call the agent places is recorded here so the dashboard can show
accurate totals that survive restarts. Google Sheets remains the contact
list; this is the operational history behind the dashboard numbers.

Lifecycle of a row:
  record_initiated()  -> when Twilio accepts the outbound call
  update_status()     -> on each Twilio status callback (ringing, in-progress, ...)
  record_result()     -> when the CallSession ends, with transcript + qualification
"""
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

# Twilio statuses that mean the call is over and nobody talked to the agent
TERMINAL_UNANSWERED = {"busy", "failed", "no-answer", "canceled"}
# Twilio statuses that mean the call is still live
IN_PROGRESS = {"queued", "initiated", "ringing", "in-progress"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    call_sid      TEXT PRIMARY KEY,
    campaign_id   TEXT NOT NULL DEFAULT '',
    contact_id    TEXT NOT NULL DEFAULT '',
    contact_name  TEXT NOT NULL DEFAULT '',
    company       TEXT NOT NULL DEFAULT '',
    phone         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'initiated',
    answered      INTEGER NOT NULL DEFAULT 0,
    duration      REAL NOT NULL DEFAULT 0,
    outcome       TEXT NOT NULL DEFAULT '',
    interest      TEXT NOT NULL DEFAULT '',
    email         TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '',
    transcript    TEXT NOT NULL DEFAULT '[]',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    ended_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_calls_created ON calls(created_at);
CREATE INDEX IF NOT EXISTS idx_calls_campaign ON calls(campaign_id);
"""

_LIST_COLUMNS = (
    "call_sid, campaign_id, contact_id, contact_name, company, phone, status, "
    "answered, duration, outcome, interest, email, summary, created_at, updated_at, ended_at"
)


class CallLog:
    """Thread-safe SQLite call history."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # One shared connection; access is serialized by the lock.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ writes

    def record_initiated(
        self,
        call_sid: str,
        phone: str = "",
        contact_name: str = "",
        company: str = "",
        campaign_id: str = "",
        contact_id: str = "",
        now: float | None = None,
    ) -> None:
        """Record a call that Twilio has accepted."""
        now = now if now is not None else time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO calls (call_sid, campaign_id, contact_id, contact_name,
                       company, phone, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'initiated', ?, ?)
                   ON CONFLICT(call_sid) DO NOTHING""",
                (call_sid, campaign_id, contact_id, contact_name, company, phone, now, now),
            )
            self._conn.commit()

    def record_failed_attempt(
        self,
        phone: str = "",
        contact_name: str = "",
        company: str = "",
        campaign_id: str = "",
        contact_id: str = "",
        reason: str = "twilio_initiate_failed",
        now: float | None = None,
    ) -> str:
        """Record a call Twilio refused to place. Returns the synthetic id used."""
        now = now if now is not None else time.time()
        call_id = f"failed_{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._conn.execute(
                """INSERT INTO calls (call_sid, campaign_id, contact_id, contact_name,
                       company, phone, status, summary, created_at, updated_at, ended_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'failed', ?, ?, ?, ?)""",
                (call_id, campaign_id, contact_id, contact_name, company, phone,
                 reason, now, now, now),
            )
            self._conn.commit()
        return call_id

    def update_status(
        self,
        call_sid: str,
        status: str,
        duration: float | None = None,
        phone: str = "",
        now: float | None = None,
    ) -> None:
        """Apply a Twilio status callback. Creates the row if we never saw it."""
        now = now if now is not None else time.time()
        status = (status or "").strip().lower()
        if not call_sid or not status:
            return
        answered = 1 if status == "in-progress" else 0
        ended = now if status in TERMINAL_UNANSWERED or status == "completed" else None
        with self._lock:
            self._conn.execute(
                """INSERT INTO calls (call_sid, phone, status, answered, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(call_sid) DO NOTHING""",
                (call_sid, phone, status, answered, now, now),
            )
            # A completed call that ever reached in-progress stays "answered".
            self._conn.execute(
                """UPDATE calls SET
                       status = ?,
                       answered = MAX(answered, ?),
                       duration = CASE WHEN ? IS NOT NULL THEN ? ELSE duration END,
                       ended_at = COALESCE(?, ended_at),
                       updated_at = ?
                   WHERE call_sid = ?""",
                (status, answered, duration, duration, ended, now, call_sid),
            )
            # Twilio reports duration > 0 only for calls that connected.
            if status == "completed" and duration:
                self._conn.execute(
                    "UPDATE calls SET answered = 1 WHERE call_sid = ?", (call_sid,)
                )
            self._conn.commit()

    def record_result(
        self,
        call_sid: str,
        duration: float,
        transcript: list[dict] | None = None,
        qualification: dict | None = None,
        now: float | None = None,
    ) -> None:
        """Store the outcome of a finished conversation."""
        now = now if now is not None else time.time()
        q = qualification or {}
        transcript = transcript or []
        # The caller spoke at least once -> the call was answered by a person.
        answered = 1 if any(t.get("role") == "user" for t in transcript) else 0
        with self._lock:
            self._conn.execute(
                """INSERT INTO calls (call_sid, status, created_at, updated_at)
                   VALUES (?, 'completed', ?, ?)
                   ON CONFLICT(call_sid) DO NOTHING""",
                (call_sid, now, now),
            )
            self._conn.execute(
                """UPDATE calls SET
                       status = 'completed',
                       answered = MAX(answered, ?),
                       duration = MAX(duration, ?),
                       outcome = ?,
                       interest = ?,
                       email = ?,
                       summary = ?,
                       transcript = ?,
                       ended_at = COALESCE(ended_at, ?),
                       updated_at = ?
                   WHERE call_sid = ?""",
                (
                    answered,
                    float(duration or 0),
                    q.get("outcome") or "",
                    q.get("interest_level") or "",
                    q.get("email_address") or "",
                    q.get("summary") or "",
                    json.dumps(transcript),
                    now,
                    now,
                    call_sid,
                ),
            )
            self._conn.commit()

    # ------------------------------------------------------------------- reads

    def get(self, call_sid: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM calls WHERE call_sid = ?", (call_sid,)
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["transcript"] = json.loads(data.get("transcript") or "[]")
        data["answered"] = bool(data["answered"])
        return data

    def list_calls(
        self,
        limit: int = 50,
        offset: int = 0,
        status: str = "",
        outcome: str = "",
        search: str = "",
        campaign_id: str = "",
    ) -> tuple[list[dict], int]:
        """Newest-first page of calls plus the total matching count."""
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        if outcome:
            where.append("outcome = ?")
            params.append(outcome)
        if campaign_id:
            where.append("campaign_id = ?")
            params.append(campaign_id)
        if search:
            like = f"%{search.strip()}%"
            where.append("(contact_name LIKE ? OR company LIKE ? OR phone LIKE ? OR email LIKE ?)")
            params.extend([like] * 4)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM calls {clause}", params
            ).fetchone()[0]
            rows = self._conn.execute(
                f"SELECT {_LIST_COLUMNS} FROM calls {clause} "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        calls = []
        for r in rows:
            d = dict(r)
            d["answered"] = bool(d["answered"])
            calls.append(d)
        return calls, total

    def summary(self, now: float | None = None) -> dict:
        """Aggregate counts used by the dashboard."""
        now = now if now is not None else time.time()
        start_of_today = datetime.fromtimestamp(now).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        in_progress = tuple(IN_PROGRESS)
        placeholders = ",".join("?" * len(in_progress))
        with self._lock:
            row = self._conn.execute(
                f"""SELECT
                       COUNT(*)                                            AS total,
                       COALESCE(SUM(answered), 0)                          AS answered,
                       COALESCE(SUM(outcome = 'qualified'), 0)             AS qualified,
                       COALESCE(SUM(outcome = 'follow_up'), 0)             AS follow_up,
                       COALESCE(SUM(email != ''), 0)                       AS emails,
                       COALESCE(SUM(status IN ('busy','failed','no-answer','canceled')), 0)
                                                                           AS failed,
                       COALESCE(SUM(status IN ({placeholders})), 0)        AS active,
                       COALESCE(SUM(created_at >= ?), 0)                   AS today,
                       COALESCE(SUM(duration), 0)                          AS seconds
                   FROM calls""",
                (*in_progress, start_of_today),
            ).fetchone()
            outcomes = self._conn.execute(
                """SELECT CASE WHEN outcome = '' THEN
                              CASE WHEN status IN ('busy','failed','no-answer','canceled')
                                   THEN status ELSE 'pending' END
                          ELSE outcome END AS label,
                          COUNT(*) AS n
                   FROM calls GROUP BY label ORDER BY n DESC"""
            ).fetchall()
        return {
            "total_calls": row["total"],
            "answered": row["answered"],
            "qualified": row["qualified"],
            "follow_up": row["follow_up"],
            "emails_collected": row["emails"],
            "failed": row["failed"],
            "active": row["active"],
            "today": row["today"],
            "talk_seconds": float(row["seconds"]),
            "outcomes": {r["label"]: r["n"] for r in outcomes},
        }

    def daily_counts(self, days: int = 14, now: float | None = None) -> list[dict]:
        """Calls/answered/qualified per local day for the last `days` days (oldest first)."""
        now = now if now is not None else time.time()
        days = max(1, min(int(days), 365))
        today = datetime.fromtimestamp(now).date()
        first = today - timedelta(days=days - 1)
        since = datetime.combine(first, datetime.min.time()).timestamp()
        with self._lock:
            rows = self._conn.execute(
                "SELECT created_at, answered, outcome FROM calls WHERE created_at >= ?",
                (since,),
            ).fetchall()
        buckets = {
            (first + timedelta(days=i)).isoformat(): {"calls": 0, "answered": 0, "qualified": 0}
            for i in range(days)
        }
        for r in rows:
            key = datetime.fromtimestamp(r["created_at"]).date().isoformat()
            if key in buckets:
                b = buckets[key]
                b["calls"] += 1
                b["answered"] += 1 if r["answered"] else 0
                b["qualified"] += 1 if r["outcome"] == "qualified" else 0
        return [{"date": d, **v} for d, v in buckets.items()]


# ------------------------------------------------------------------ singleton

_instance: CallLog | None = None
_instance_lock = threading.Lock()


def get_call_log() -> CallLog:
    """Process-wide CallLog at settings.data_dir/voice_agent.db."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                from app.config import settings

                _instance = CallLog(Path(settings.data_dir) / "voice_agent.db")
    return _instance


def set_call_log(log: CallLog | None) -> None:
    """Swap the process-wide CallLog (used by tests)."""
    global _instance
    _instance = log
