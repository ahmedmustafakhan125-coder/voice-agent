"""Unit tests for the SQLite call log and its wiring into the app."""
import asyncio
from datetime import datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.storage.call_log import CallLog

NOON = datetime(2026, 11, 10, 12, 0, 0).timestamp()
DAY = 86400


def _qual(outcome="qualified", email="joe@trucks.com"):
    return {
        "outcome": outcome,
        "interest_level": "interested",
        "email_address": email,
        "summary": "Wants info.",
    }


# ------------------------------------------------------------- store basics

def test_record_initiated_and_get():
    log = CallLog(":memory:")
    log.record_initiated("CA1", phone="+15550001", contact_name="Joe", company="JT", now=NOON)
    call = log.get("CA1")
    assert call["status"] == "initiated"
    assert call["contact_name"] == "Joe"
    assert call["answered"] is False
    assert call["transcript"] == []


def test_get_unknown_call_returns_none():
    assert CallLog(":memory:").get("nope") is None


def test_record_initiated_is_idempotent():
    log = CallLog(":memory:")
    log.record_initiated("CA1", contact_name="Joe", now=NOON)
    log.record_initiated("CA1", contact_name="Someone else", now=NOON + 5)
    assert log.get("CA1")["contact_name"] == "Joe"


def test_status_flow_marks_answered_and_duration():
    log = CallLog(":memory:")
    log.record_initiated("CA1", now=NOON)
    log.update_status("CA1", "ringing", now=NOON + 1)
    log.update_status("CA1", "in-progress", now=NOON + 5)
    log.update_status("CA1", "completed", duration=42, now=NOON + 50)
    call = log.get("CA1")
    assert call["status"] == "completed"
    assert call["answered"] is True
    assert call["duration"] == 42
    assert call["ended_at"] == NOON + 50


def test_unanswered_terminal_status():
    log = CallLog(":memory:")
    log.record_initiated("CA1", now=NOON)
    log.update_status("CA1", "no-answer", duration=0, now=NOON + 30)
    call = log.get("CA1")
    assert call["answered"] is False
    assert call["ended_at"] == NOON + 30


def test_update_status_for_unknown_call_creates_row():
    log = CallLog(":memory:")
    log.update_status("CA9", "ringing", phone="+15559999", now=NOON)
    assert log.get("CA9")["phone"] == "+15559999"


def test_update_status_ignores_blank_input():
    log = CallLog(":memory:")
    log.update_status("", "ringing")
    log.update_status("CA1", "")
    assert log.list_calls()[1] == 0


def test_record_result_stores_qualification_and_transcript():
    log = CallLog(":memory:")
    log.record_initiated("CA1", now=NOON)
    transcript = [{"role": "assistant", "text": "Hi"}, {"role": "user", "text": "Hey"}]
    log.record_result("CA1", duration=61.5, transcript=transcript, qualification=_qual(), now=NOON + 70)
    call = log.get("CA1")
    assert call["outcome"] == "qualified"
    assert call["email"] == "joe@trucks.com"
    assert call["interest"] == "interested"
    assert call["answered"] is True
    assert call["duration"] == 61.5
    assert call["transcript"] == transcript


def test_record_result_without_user_speech_is_not_answered():
    log = CallLog(":memory:")
    log.record_result("CA1", duration=8, transcript=[{"role": "assistant", "text": "Hi"}])
    assert log.get("CA1")["answered"] is False


def test_record_result_handles_missing_qualification():
    log = CallLog(":memory:")
    log.record_result("CA1", duration=3, transcript=None, qualification=None)
    call = log.get("CA1")
    assert call["outcome"] == ""
    assert call["status"] == "completed"


def test_failed_attempt_gets_synthetic_id():
    log = CallLog(":memory:")
    cid = log.record_failed_attempt(phone="+1555", contact_name="Al")
    assert cid.startswith("failed_")
    assert log.get(cid)["status"] == "failed"


# ------------------------------------------------------------ list + stats

def _seed(log):
    log.record_initiated("A", contact_name="Alice", company="Acme Haul", phone="+1111",
                         campaign_id="c1", now=NOON - DAY)
    log.update_status("A", "completed", duration=60, now=NOON - DAY + 60)
    log.record_result("A", 60, [{"role": "user", "text": "yes"}], _qual(), now=NOON - DAY + 61)

    log.record_initiated("B", contact_name="Bob", company="Bob Freight", phone="+2222",
                         campaign_id="c1", now=NOON)
    log.update_status("B", "busy", now=NOON + 10)

    log.record_initiated("C", contact_name="Cara", phone="+3333", campaign_id="c2", now=NOON + 20)
    log.update_status("C", "ringing", now=NOON + 21)

    log.record_initiated("D", contact_name="Dan", phone="+4444", campaign_id="c2", now=NOON + 30)
    log.record_result("D", 30, [{"role": "user", "text": "maybe"}], _qual("follow_up", None),
                      now=NOON + 60)


def test_list_calls_newest_first_with_total():
    log = CallLog(":memory:")
    _seed(log)
    calls, total = log.list_calls(limit=2)
    assert total == 4
    assert [c["call_sid"] for c in calls] == ["D", "C"]
    assert "transcript" not in calls[0]


def test_list_calls_filters():
    log = CallLog(":memory:")
    _seed(log)
    assert log.list_calls(status="busy")[1] == 1
    assert log.list_calls(outcome="qualified")[0][0]["call_sid"] == "A"
    assert log.list_calls(campaign_id="c2")[1] == 2
    assert log.list_calls(search="freight")[0][0]["call_sid"] == "B"
    assert log.list_calls(search="+333")[0][0]["call_sid"] == "C"


def test_list_calls_clamps_paging():
    log = CallLog(":memory:")
    _seed(log)
    calls, _ = log.list_calls(limit=0, offset=-5)
    assert len(calls) == 1


def test_summary_counts():
    log = CallLog(":memory:")
    _seed(log)
    s = log.summary(now=NOON + 100)
    assert s["total_calls"] == 4
    assert s["answered"] == 2          # A (duration) and D (user spoke)
    assert s["qualified"] == 1
    assert s["follow_up"] == 1
    assert s["emails_collected"] == 1
    assert s["failed"] == 1            # B busy
    assert s["active"] == 1            # C ringing
    assert s["today"] == 3             # B, C, D
    assert s["talk_seconds"] == 90
    assert s["outcomes"] == {"qualified": 1, "follow_up": 1, "busy": 1, "pending": 1}


def test_summary_empty_db():
    s = CallLog(":memory:").summary()
    assert s["total_calls"] == 0
    assert s["talk_seconds"] == 0
    assert s["outcomes"] == {}


def test_daily_counts_buckets_and_order():
    log = CallLog(":memory:")
    _seed(log)
    days = log.daily_counts(days=3, now=NOON + 100)
    assert [d["date"] for d in days] == ["2026-11-08", "2026-11-09", "2026-11-10"]
    assert days[0] == {"date": "2026-11-08", "calls": 0, "answered": 0, "qualified": 0}
    assert days[1]["calls"] == 1 and days[1]["qualified"] == 1
    assert days[2]["calls"] == 3 and days[2]["answered"] == 1


def test_persists_to_disk(tmp_path):
    path = tmp_path / "sub" / "calls.db"
    log = CallLog(path)
    log.record_initiated("CA1", contact_name="Joe")
    log.close()
    reopened = CallLog(path)
    assert reopened.get("CA1")["contact_name"] == "Joe"
    reopened.close()


# ------------------------------------------------------------------ wiring

def test_webhook_updates_call_log(call_log):
    from app.main import app

    client = TestClient(app)
    res = client.post(
        "/webhooks/twilio/status",
        data={"CallSid": "CA7", "CallStatus": "completed", "To": "+1555", "CallDuration": "33"},
    )
    assert res.status_code == 200
    call = call_log.get("CA7")
    assert call["status"] == "completed"
    assert call["duration"] == 33
    assert call["answered"] is True


def test_initiate_outbound_call_logs_success(call_log):
    from app.telephony import twilio_client

    fake = MagicMock()
    fake.calls.create.return_value = MagicMock(sid="CA42")
    with patch.object(twilio_client, "get_twilio_client", return_value=fake):
        sid = twilio_client.initiate_outbound_call(
            "+15551234", campaign_id="c1", contact_id="2",
            contact_name="Joe", contact_company="JT",
        )
    assert sid == "CA42"
    call = call_log.get("CA42")
    assert (call["phone"], call["contact_name"], call["company"], call["campaign_id"]) == (
        "+15551234", "Joe", "JT", "c1",
    )


def test_initiate_outbound_call_logs_failure(call_log):
    from app.telephony import twilio_client

    with patch.object(twilio_client, "get_twilio_client", side_effect=RuntimeError("bad creds")):
        assert twilio_client.initiate_outbound_call("+15551234", contact_name="Joe") is None
    calls, total = call_log.list_calls()
    assert total == 1 and calls[0]["status"] == "failed"


def test_session_cleanup_records_result(call_log):
    from app.pipeline.session import CallSession

    session = CallSession(websocket=MagicMock(), stream_sid="MZ1", call_sid="CA5",
                          contact_name="Joe")
    session.stt.close = _async_noop
    session.tts.close = _async_noop
    session._start_time = 1.0
    session._conversation_transcript = [
        {"role": "assistant", "text": "Hi Joe"},
        {"role": "user", "text": "Sure, email me at joe@x.com"},
    ]

    async def fake_qualify(transcript):
        return _qual(email="joe@x.com")

    session.llm.qualify_lead = fake_qualify
    asyncio.run(session._cleanup())

    call = call_log.get("CA5")
    assert call["outcome"] == "qualified"
    assert call["email"] == "joe@x.com"
    assert len(call["transcript"]) == 2


async def _async_noop(*args, **kwargs):
    return None
