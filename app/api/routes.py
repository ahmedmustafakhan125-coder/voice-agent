"""REST API routes for campaign management and dashboard.

Endpoints:
  POST   /campaigns              — Start a new campaign
  GET    /campaigns              — List all campaigns
  GET    /campaigns/{id}         — Get campaign status/stats
  POST   /campaigns/{id}/pause   — Pause a campaign
  POST   /campaigns/{id}/resume  — Resume a campaign
  POST   /campaigns/{id}/stop    — Stop a campaign
  POST   /calls/single           — Make a single test call
  GET    /stats                  — Global stats
"""
import asyncio
import logging
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.automation.email_sender import send_follow_up_email
from app.campaign.dialer import (
    CampaignConfig,
    CampaignDialer,
    get_all_campaigns,
    get_campaign,
)
from app.telephony.twilio_client import initiate_outbound_call

router = APIRouter()
logger = logging.getLogger(__name__)


# --- Request/Response models ---

class StartCampaignRequest(BaseModel):
    max_concurrent_calls: int = 1
    delay_between_calls: float = 5.0
    max_calls: int = 0


class SingleCallRequest(BaseModel):
    phone_number: str
    contact_name: str = ""
    company: str = ""


class SendEmailRequest(BaseModel):
    to_email: str
    contact_name: str = ""


# --- Campaign endpoints ---

@router.post("/campaigns")
async def start_campaign(req: StartCampaignRequest):
    """Start a new outbound calling campaign."""
    campaign_id = f"camp_{uuid.uuid4().hex[:8]}"

    config = CampaignConfig(
        campaign_id=campaign_id,
        max_concurrent_calls=req.max_concurrent_calls,
        delay_between_calls=req.delay_between_calls,
        max_calls=req.max_calls,
    )

    dialer = CampaignDialer(config)
    await dialer.start()

    return {
        "campaign_id": campaign_id,
        "status": "running",
        "config": {
            "max_concurrent_calls": req.max_concurrent_calls,
            "delay_between_calls": req.delay_between_calls,
            "max_calls": req.max_calls,
        },
    }


@router.get("/campaigns")
async def list_campaigns():
    """List all active campaigns and their stats."""
    campaigns = get_all_campaigns()
    return {
        "campaigns": [
            {
                "campaign_id": cid,
                "status": dialer.stats.status,
                "total_contacts": dialer.stats.total_contacts,
                "calls_made": dialer.stats.calls_made,
                "calls_answered": dialer.stats.calls_answered,
                "qualified_leads": dialer.stats.qualified_leads,
                "active_calls": dialer.stats.active_calls,
            }
            for cid, dialer in campaigns.items()
        ]
    }


@router.get("/campaigns/{campaign_id}")
async def get_campaign_status(campaign_id: str):
    """Get detailed status for a specific campaign."""
    dialer = get_campaign(campaign_id)
    if not dialer:
        raise HTTPException(status_code=404, detail="Campaign not found")

    stats = dialer.stats
    return {
        "campaign_id": stats.campaign_id,
        "status": stats.status,
        "total_contacts": stats.total_contacts,
        "calls_made": stats.calls_made,
        "calls_answered": stats.calls_answered,
        "calls_failed": stats.calls_failed,
        "qualified_leads": stats.qualified_leads,
        "active_calls": stats.active_calls,
        "started_at": stats.started_at,
        "finished_at": stats.finished_at,
        "errors": stats.errors,
    }


@router.post("/campaigns/{campaign_id}/pause")
async def pause_campaign(campaign_id: str):
    """Pause a running campaign."""
    dialer = get_campaign(campaign_id)
    if not dialer:
        raise HTTPException(status_code=404, detail="Campaign not found")

    dialer.pause()
    return {"campaign_id": campaign_id, "status": "paused"}


@router.post("/campaigns/{campaign_id}/resume")
async def resume_campaign(campaign_id: str):
    """Resume a paused campaign."""
    dialer = get_campaign(campaign_id)
    if not dialer:
        raise HTTPException(status_code=404, detail="Campaign not found")

    dialer.resume()
    return {"campaign_id": campaign_id, "status": "running"}


@router.post("/campaigns/{campaign_id}/stop")
async def stop_campaign(campaign_id: str):
    """Stop a running campaign."""
    dialer = get_campaign(campaign_id)
    if not dialer:
        raise HTTPException(status_code=404, detail="Campaign not found")

    await dialer.stop()
    return {"campaign_id": campaign_id, "status": "stopping"}


# --- Single call endpoint ---

@router.post("/calls/single")
async def make_single_call(req: SingleCallRequest):
    """Make a single outbound test call."""
    call_sid = initiate_outbound_call(
        to_number=req.phone_number,
        campaign_id="manual",
        contact_id="manual",
        contact_name=req.contact_name,
        contact_company=req.company,
    )

    if not call_sid:
        raise HTTPException(status_code=500, detail="Failed to initiate call")

    return {
        "call_sid": call_sid,
        "phone_number": req.phone_number,
        "status": "initiated",
    }


# --- Email endpoint ---

@router.post("/email/send-followup")
async def send_followup(req: SendEmailRequest):
    """Send a follow-up email to a lead."""
    success = await send_follow_up_email(req.to_email, req.contact_name)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to send email")

    return {"status": "sent", "to": req.to_email}


# --- Stats endpoint ---

@router.get("/stats")
async def global_stats():
    """Get aggregate stats across all campaigns."""
    campaigns = get_all_campaigns()

    total_calls = sum(d.stats.calls_made for d in campaigns.values())
    total_answered = sum(d.stats.calls_answered for d in campaigns.values())
    total_qualified = sum(d.stats.qualified_leads for d in campaigns.values())
    total_failed = sum(d.stats.calls_failed for d in campaigns.values())
    active_campaigns = sum(
        1 for d in campaigns.values() if d.stats.status == "running"
    )

    return {
        "active_campaigns": active_campaigns,
        "total_campaigns": len(campaigns),
        "total_calls": total_calls,
        "total_answered": total_answered,
        "total_qualified": total_qualified,
        "total_failed": total_failed,
    }
