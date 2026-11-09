"""Twilio REST API client for initiating outbound calls."""
import logging
from urllib.parse import urlencode

from twilio.rest import Client

from app.config import settings
from app.storage.call_log import get_call_log

logger = logging.getLogger(__name__)


def get_twilio_client() -> Client:
    """Create a Twilio REST client using configured credentials."""
    return Client(settings.twilio_account_sid, settings.twilio_auth_token)


def initiate_outbound_call(
    to_number: str,
    campaign_id: str = "",
    contact_id: str = "",
    agent_config_id: str = "",
    contact_name: str = "",
    contact_company: str = "",
) -> str | None:
    """Initiate an outbound call via Twilio.

    Args:
        to_number: Phone number to call (E.164 format)
        campaign_id: Campaign ID for tracking
        contact_id: Contact ID for tracking
        agent_config_id: Agent config to use for this call
        contact_name: Name of the person being called
        contact_company: Company of the person being called

    Returns:
        The Twilio Call SID if successful, None otherwise.
    """
    try:
        client = get_twilio_client()

        # Build TwiML URL with custom parameters
        params = urlencode({
            "campaign_id": campaign_id,
            "contact_id": contact_id,
            "agent_config_id": agent_config_id,
            "contact_name": contact_name,
            "contact_company": contact_company,
        })
        twiml_url = f"{settings.public_url}/twiml/voice?{params}"

        # Status callback URL for call events
        status_url = f"{settings.public_url}/webhooks/twilio/status"

        call = client.calls.create(
            to=to_number,
            from_=settings.twilio_phone_number,
            url=twiml_url,
            status_callback=status_url,
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
        )

        logger.info(
            "Outbound call initiated: sid=%s to=%s campaign=%s",
            call.sid, to_number, campaign_id,
        )
        _log_safely(
            "record_initiated",
            call_sid=call.sid,
            phone=to_number,
            contact_name=contact_name,
            company=contact_company,
            campaign_id=campaign_id,
            contact_id=contact_id,
        )
        return call.sid

    except Exception:
        logger.exception("Failed to initiate outbound call to %s", to_number)
        _log_safely(
            "record_failed_attempt",
            phone=to_number,
            contact_name=contact_name,
            company=contact_company,
            campaign_id=campaign_id,
            contact_id=contact_id,
        )
        return None


def _log_safely(method: str, **kwargs) -> None:
    """Write to the call log without ever breaking call placement."""
    try:
        getattr(get_call_log(), method)(**kwargs)
    except Exception:
        logger.exception("Call log write failed (%s)", method)
