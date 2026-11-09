"""Unit tests for the dashboard API."""
import pytest
from fastapi.testclient import TestClient

from app.api.dashboard import build_overview
from app.campaign import dialer as dialer_mod
from app.campaign.dialer import CampaignConfig, CampaignDialer


@pytest.fixture
def client(call_log):
    from app.main import app

    return TestClient(app)


def _seed(log):
    log.record_initiated("A", contact_name="Alice", company="Acme", phone="+1111", campaign_id="c1")
    log.update_status("A", "in-progress")
    log.record_result(
        "A", 120,
        [{"role": "assistant", "text": "Hi"}, {"role": "user", "text": "Sure"}],
        {"outcome": "qualified", "email_address": "a@acme.com", "interest_level": "interested",
         "summary": "Interested"},
    )
    log.record_initiated("B", contact_name="Bob", phone="+2222", campaign_id="c1")
    log.update_status("B", "no-answer")
    log.record_initiated("C", contact_name="Cara", phone="+3333", campaign_id="c2")
    log.record_result("C", 60, [{"role": "user", "text": "no thanks"}],
                      {"outcome": "not_interested"})


# ------------------------------------------------------------ pure shaping

def _summary(**over):
    base = dict(total_calls=10, today=4, active=1, answered=5, failed=3, qualified=2,
                follow_up=1, emails_collected=2, talk_seconds=600.0, outcomes={"qualified": 2})
    base.update(over)
    return base


def test_build_overview_rates_and_cost():
    o = build_overview(_summary(), daily=[], campaigns=[])
    assert o["calls"] == {"total": 10, "today": 4, "active": 1, "answered": 5, "failed": 3}
    assert o["rates"] == {"answer_rate": 0.5, "qualify_rate": 0.4}
    cost = o["cost"]
    assert cost["talk_minutes"] == 10.0
    assert cost["estimated_cost"] == 0.2      # 10 min * $0.02
    assert cost["industry_cost"] == 0.9       # 10 min * $0.09
    assert cost["saved"] == 0.7
    assert cost["avg_call_seconds"] == 120


def test_build_overview_handles_zero_calls():
    o = build_overview(_summary(total_calls=0, answered=0, qualified=0, talk_seconds=0.0),
                       daily=[], campaigns=[])
    assert o["rates"] == {"answer_rate": 0.0, "qualify_rate": 0.0}
    assert o["cost"]["avg_call_seconds"] == 0
    assert o["cost"]["saved"] == 0


# ---------------------------------------------------------------- endpoints

def test_overview_endpoint(client, call_log):
    _seed(call_log)
    res = client.get("/api/dashboard/overview?days=7")
    assert res.status_code == 200
    body = res.json()
    assert body["calls"]["total"] == 3
    assert body["calls"]["answered"] == 2
    assert body["calls"]["failed"] == 1
    assert body["leads"]["qualified"] == 1
    assert body["cost"]["talk_minutes"] == 3.0
    assert len(body["daily"]) == 7
    assert body["daily"][-1]["calls"] == 3
    assert body["outcomes"]["qualified"] == 1


def test_overview_rejects_bad_days(client):
    assert client.get("/api/dashboard/overview?days=0").status_code == 422
    assert client.get("/api/dashboard/overview?days=1000").status_code == 422


def test_overview_includes_live_campaigns(client):
    d = CampaignDialer(CampaignConfig(campaign_id="camp_live"))
    d.stats.status = "running"
    d.stats.calls_made = 7
    dialer_mod._active_campaigns["camp_live"] = d
    try:
        body = client.get("/api/dashboard/overview").json()
    finally:
        dialer_mod._active_campaigns.clear()
    assert body["campaigns"][0]["campaign_id"] == "camp_live"
    assert body["campaigns"][0]["calls_made"] == 7


def test_list_calls_endpoint_paging_and_filters(client, call_log):
    _seed(call_log)
    body = client.get("/api/calls?limit=2").json()
    assert body["total"] == 3
    assert [c["call_sid"] for c in body["calls"]] == ["C", "B"]

    assert client.get("/api/calls?outcome=qualified").json()["calls"][0]["call_sid"] == "A"
    assert client.get("/api/calls?status=no-answer").json()["total"] == 1
    assert client.get("/api/calls?search=acme").json()["total"] == 1
    assert client.get("/api/calls?campaign_id=c2").json()["calls"][0]["contact_name"] == "Cara"
    assert client.get("/api/calls?limit=500").status_code == 422


def test_call_detail_includes_transcript(client, call_log):
    _seed(call_log)
    body = client.get("/api/calls/A").json()
    assert body["email"] == "a@acme.com"
    assert body["transcript"][1] == {"role": "user", "text": "Sure"}


def test_call_detail_404(client):
    assert client.get("/api/calls/nope").status_code == 404


def test_global_stats_survive_campaign_end(client, call_log):
    """/api/stats used to reset to 0 once campaigns finished."""
    _seed(call_log)
    body = client.get("/api/stats").json()
    assert body["total_calls"] == 3
    assert body["total_answered"] == 2
    assert body["total_qualified"] == 1
    assert body["total_failed"] == 1
