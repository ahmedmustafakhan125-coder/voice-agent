"""SendGrid email client for sending follow-up emails to qualified leads.

Sends a templated email with dispatch service info after a call
where the trucker expressed interest and provided an email.
"""
import asyncio
import logging

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Email, To, Content

from app.config import settings

logger = logging.getLogger(__name__)

FOLLOW_UP_SUBJECT = "Great talking with you — National Dispatch info inside"

FOLLOW_UP_HTML = """
<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
    <h2 style="color: #1a365d;">Hey {name}!</h2>

    <p>It was great chatting with you today. As I mentioned on the phone, we'd love to help
    you find the best-paying loads and take the paperwork off your plate.</p>

    <h3 style="color: #2d3748;">What we do for our drivers:</h3>
    <ul style="color: #4a5568; line-height: 1.8;">
        <li>Find top-paying loads that match your lanes and equipment</li>
        <li>Handle all broker negotiations and rate confirmations</li>
        <li>24/7 dispatcher support while you're on the road</li>
        <li>Load planning and route optimization</li>
        <li>Paperwork and invoicing handled for you</li>
    </ul>

    <p>Our drivers typically see <strong>15-20% higher revenue</strong> within the first month.</p>

    <p>If you're ready to get started, just reply to this email or give us a call and we'll
    get you set up right away.</p>

    <p style="margin-top: 30px;">
        Talk soon,<br>
        <strong>Sarah</strong><br>
        National Dispatch<br>
    </p>
</div>
"""

FOLLOW_UP_TEXT = """Hey {name}!

It was great chatting with you today. As I mentioned on the phone, we'd love to help you find the best-paying loads and take the paperwork off your plate.

What we do for our drivers:
- Find top-paying loads that match your lanes and equipment
- Handle all broker negotiations and rate confirmations
- 24/7 dispatcher support while you're on the road
- Load planning and route optimization
- Paperwork and invoicing handled for you

Our drivers typically see 15-20% higher revenue within the first month.

If you're ready to get started, just reply to this email or give us a call and we'll get you set up right away.

Talk soon,
Sarah
National Dispatch
"""


async def send_follow_up_email(
    to_email: str,
    contact_name: str = "there",
) -> bool:
    """Send a follow-up email to a qualified lead.

    Args:
        to_email: The trucker's email address.
        contact_name: The trucker's name for personalization.

    Returns:
        True if sent successfully, False otherwise.
    """
    if not settings.sendgrid_api_key:
        logger.warning("SendGrid API key not configured, skipping email")
        return False

    name = contact_name if contact_name else "there"

    message = Mail(
        from_email=Email(settings.sendgrid_from_email, "Sarah at National Dispatch"),
        to_emails=To(to_email),
        subject=FOLLOW_UP_SUBJECT,
    )
    message.content = [
        Content("text/plain", FOLLOW_UP_TEXT.format(name=name)),
        Content("text/html", FOLLOW_UP_HTML.format(name=name)),
    ]

    try:
        sg = SendGridAPIClient(settings.sendgrid_api_key)
        response = await asyncio.to_thread(sg.send, message)
        logger.info(
            "Follow-up email sent to %s (status=%d)",
            to_email, response.status_code,
        )
        return response.status_code in (200, 201, 202)

    except Exception:
        logger.exception("Failed to send follow-up email to %s", to_email)
        return False
