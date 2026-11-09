"""Dashboard API — numbers and call history for the web dashboard.

  GET /dashboard/overview   KPIs, cost vs industry, daily trend, outcomes, campaigns
  GET /calls                Paginated, filterable call history
  GET /calls/{call_sid}     One call with its transcript
"""
from fastapi import APIRouter, HTTPException, Query

from app.campaign.dialer import get_all_campaigns
from app.config import settings
from app.storage.call_log import get_call_log

router = APIRouter()


def _rate(part: float, whole: float) -> float:
    return round(part / whole, 4) if whole else 0.0


def build_overview(summary: dict, daily: list[dict], campaigns: list[dict]) -> dict:
    """Shape raw call-log aggregates into the dashboard payload (pure function)."""
    minutes = summary["talk_seconds"] / 60
    our_cost = minutes * settings.cost_per_minute
    industry_cost = minutes * settings.industry_cost_per_minute
    return {
        "calls": {
            "total": summary["total_calls"],
            "today": summary["today"],
            "active": summary["active"],
            "answered": summary["answered"],
            "failed": summary["failed"],
        },
        "leads": {
            "qualified": summary["qualified"],
            "follow_up": summary["follow_up"],
            "emails_collected": summary["emails_collected"],
        },
        "rates": {
            "answer_rate": _rate(summary["answered"], summary["total_calls"]),
            "qualify_rate": _rate(summary["qualified"], summary["answered"]),
        },
        "cost": {
            "talk_minutes": round(minutes, 1),
            "cost_per_minute": settings.cost_per_minute,
            "industry_cost_per_minute": settings.industry_cost_per_minute,
            "estimated_cost": round(our_cost, 2),
            "industry_cost": round(industry_cost, 2),
            "saved": round(industry_cost - our_cost, 2),
            "avg_call_seconds": round(summary["talk_seconds"] / summary["answered"])
            if summary["answered"] else 0,
        },
        "outcomes": summary["outcomes"],
        "daily": daily,
        "campaigns": campaigns,
    }


def _campaign_rows() -> list[dict]:
    return [
        {
            "campaign_id": cid,
            "status": d.stats.status,
            "total_contacts": d.stats.total_contacts,
            "calls_made": d.stats.calls_made,
            "calls_answered": d.stats.calls_answered,
            "qualified_leads": d.stats.qualified_leads,
            "active_calls": d.stats.active_calls,
            "started_at": d.stats.started_at,
        }
        for cid, d in get_all_campaigns().items()
    ]


@router.get("/dashboard/overview")
async def dashboard_overview(days: int = Query(14, ge=1, le=90)):
    log = get_call_log()
    return build_overview(log.summary(), log.daily_counts(days), _campaign_rows())


@router.get("/calls")
async def list_calls(
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str = "",
    outcome: str = "",
    search: str = "",
    campaign_id: str = "",
):
    calls, total = get_call_log().list_calls(
        limit=limit, offset=offset, status=status, outcome=outcome,
        search=search, campaign_id=campaign_id,
    )
    return {"calls": calls, "total": total, "limit": limit, "offset": offset}


@router.get("/calls/{call_sid}")
async def get_call(call_sid: str):
    call = get_call_log().get(call_sid)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    return call
