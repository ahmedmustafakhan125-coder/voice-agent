"""Unit tests for the campaign dialer's call lifecycle.

Google Sheets and Twilio are replaced with in-memory fakes.
"""
import asyncio
import itertools
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.campaign import dialer as dialer_mod
from app.campaign.dialer import CampaignConfig, CampaignDialer, notify_call_status


class FakeSheets:
    def __init__(self, contacts):
        self.contacts = contacts
        self.calling, self.results, self.failed = [], [], []

    async def get_pending_contacts(self, limit=50):
        return self.contacts[:limit]

    async def mark_calling(self, row, call_sid):
        self.calling.append((row, call_sid))

    async def write_result(self, row, result):
        self.results.append((row, result))

    async def mark_failed(self, row, reason="call_failed"):
        self.failed.append((row, reason))


CONTACTS = [
    {"row": 2, "name": "Joe", "phone": "+15550002", "company": "JT"},
    {"row": 3, "name": "Ann", "phone": "+15550003", "company": "AT"},
]


@pytest.fixture
def fake_env():
    """Patch Sheets + Twilio inside the dialer module."""
    sheets = FakeSheets([dict(c) for c in CONTACTS])
    sids = (f"CA{i}" for i in itertools.count(1))
    dialed = []

    def fake_initiate(to_number, **kwargs):
        dialed.append((to_number, kwargs))
        return next(sids)

    with patch.object(dialer_mod, "SheetsClient", return_value=sheets), \
         patch.object(dialer_mod, "initiate_outbound_call", side_effect=fake_initiate):
        yield sheets, dialed
    dialer_mod._active_campaigns.clear()


def _config(**overrides):
    base = dict(campaign_id="camp_t", max_concurrent_calls=1, delay_between_calls=0,
                call_timeout=5, result_grace_seconds=0.05)
    base.update(overrides)
    return CampaignConfig(**base)


async def _wait_until(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def _result(call_sid, row, outcome="qualified"):
    return {
        "call_sid": call_sid,
        "contact_id": str(row),
        "duration": 40,
        "qualification": {"outcome": outcome},
    }


async def test_results_are_written_before_campaign_completes(fake_env):
    sheets, dialed = fake_env
    d = CampaignDialer(_config())
    await d.start()

    # Only one call at a time: the second contact waits for the first result.
    await _wait_until(lambda: len(sheets.calling) == 1)
    await asyncio.sleep(0.05)
    assert len(dialed) == 1
    assert d.stats.active_calls == 1

    await d.get_results_queue().put(_result("CA1", 2))
    await _wait_until(lambda: len(sheets.calling) == 2)
    await d.get_results_queue().put(_result("CA2", 3, outcome="not_interested"))

    await asyncio.wait_for(d._task, timeout=2)
    assert d.stats.status == "completed"
    assert [r[0] for r in sheets.results] == [2, 3]
    assert d.stats.qualified_leads == 1
    assert d.stats.calls_answered == 2
    assert d.stats.active_calls == 0


async def test_concurrency_allows_parallel_calls(fake_env):
    sheets, dialed = fake_env
    d = CampaignDialer(_config(max_concurrent_calls=2))
    await d.start()
    await _wait_until(lambda: len(sheets.calling) == 2)
    assert d.stats.active_calls == 2
    for sid, row in (("CA1", 2), ("CA2", 3)):
        await d.get_results_queue().put(_result(sid, row))
    await asyncio.wait_for(d._task, timeout=2)
    assert d.stats.status == "completed"


async def test_unanswered_call_marks_row_failed_and_moves_on(fake_env):
    sheets, _ = fake_env
    d = CampaignDialer(_config())
    await d.start()
    await _wait_until(lambda: len(sheets.calling) == 1)

    notify_call_status("CA1", "busy")
    await _wait_until(lambda: len(sheets.calling) == 2)
    assert sheets.failed == [(2, "busy")]

    notify_call_status("CA2", "no-answer")
    await asyncio.wait_for(d._task, timeout=2)
    assert sheets.failed == [(2, "busy"), (3, "no-answer")]
    assert sheets.results == []


async def test_completed_without_result_is_released_after_grace(fake_env):
    sheets, _ = fake_env
    sheets.contacts = sheets.contacts[:1]
    d = CampaignDialer(_config())
    await d.start()
    await _wait_until(lambda: len(sheets.calling) == 1)

    notify_call_status("CA1", "completed")
    await asyncio.wait_for(d._task, timeout=2)
    assert sheets.failed == [(2, "no_conversation")]


async def test_result_within_grace_period_wins(fake_env):
    sheets, _ = fake_env
    sheets.contacts = sheets.contacts[:1]
    d = CampaignDialer(_config(result_grace_seconds=0.3))
    await d.start()
    await _wait_until(lambda: len(sheets.calling) == 1)

    notify_call_status("CA1", "completed")
    await d.get_results_queue().put(_result("CA1", 2))
    await asyncio.wait_for(d._task, timeout=2)
    await asyncio.sleep(0.4)  # let the grace timer fire; it must be a no-op
    assert sheets.failed == []
    assert len(sheets.results) == 1


async def test_call_timeout_frees_the_slot(fake_env):
    sheets, _ = fake_env
    sheets.contacts = sheets.contacts[:1]
    d = CampaignDialer(_config(call_timeout=0.1))
    await d.start()
    await asyncio.wait_for(d._task, timeout=2)
    assert d.stats.status == "completed"
    assert d.stats.active_calls == 0


async def test_status_for_unknown_call_is_ignored(fake_env):
    d = CampaignDialer(_config())
    d.on_call_status("CA999", "busy")  # must not raise or create tasks
    assert d._pending == {}


def test_media_stream_routes_result_to_campaign(fake_env):
    """twilio_ws must hand the campaign's results queue to the CallSession."""
    from app.main import app
    from app.telephony import twilio_ws

    created = []

    class FakeSession:
        def __init__(self, **kwargs):
            self.campaign_id = kwargs["campaign_id"]
            self.on_call_complete = None
            created.append(self)

        async def run(self):
            return None

        async def stop(self):
            return None

    d = CampaignDialer(_config(campaign_id="camp_ws"))
    dialer_mod._active_campaigns["camp_ws"] = d

    with patch.object(twilio_ws, "CallSession", FakeSession):
        client = TestClient(app)
        with client.websocket_connect("/ws/media-stream") as ws:
            ws.send_text(json.dumps({
                "event": "start",
                "start": {
                    "streamSid": "MZ1",
                    "callSid": "CA1",
                    "customParameters": {"campaign_id": "camp_ws", "contact_id": "2"},
                },
            }))
            ws.send_text(json.dumps({"event": "stop"}))

    assert created and created[0].on_call_complete is d.get_results_queue()
