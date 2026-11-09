"""Campaign dialer — async, rate-limited outbound calling engine.

Reads pending contacts from Google Sheets, initiates calls via Twilio,
runs the voice agent pipeline, and writes results back to the sheet.

Respects configurable concurrency and pacing to avoid Twilio rate limits
and keep costs predictable.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field

from app.automation.sheets import SheetsClient
from app.storage.call_log import TERMINAL_UNANSWERED
from app.telephony.twilio_client import initiate_outbound_call

logger = logging.getLogger(__name__)


@dataclass
class CampaignConfig:
    """Configuration for a dialing campaign."""
    campaign_id: str
    max_concurrent_calls: int = 1       # How many calls at once
    delay_between_calls: float = 5.0    # Seconds between starting calls
    max_calls: int = 0                  # 0 = unlimited (call all pending)
    agent_config_id: str = ""           # Custom agent config (future)
    call_timeout: float = 900.0         # Max seconds to wait for a call's result
    result_grace_seconds: float = 30.0  # Wait after Twilio "completed" for the result


@dataclass
class CampaignStats:
    """Live stats for a running campaign."""
    campaign_id: str = ""
    status: str = "idle"                # idle, running, paused, completed, error
    total_contacts: int = 0
    calls_made: int = 0
    calls_answered: int = 0
    calls_failed: int = 0
    qualified_leads: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    active_calls: int = 0
    errors: list[str] = field(default_factory=list)


# Global registry of running campaigns
_active_campaigns: dict[str, "CampaignDialer"] = {}


def get_campaign(campaign_id: str) -> "CampaignDialer | None":
    return _active_campaigns.get(campaign_id)


def get_all_campaigns() -> dict[str, "CampaignDialer"]:
    return _active_campaigns.copy()


def notify_call_status(call_sid: str, status: str) -> None:
    """Forward a Twilio status callback to whichever campaign owns the call."""
    for dialer in list(_active_campaigns.values()):
        dialer.on_call_status(call_sid, status)


class CampaignDialer:
    """Async campaign dialer that orchestrates outbound calling."""

    def __init__(self, config: CampaignConfig):
        self.config = config
        self.stats = CampaignStats(campaign_id=config.campaign_id)
        self.sheets = SheetsClient()
        self._call_results: asyncio.Queue = asyncio.Queue()
        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # Not paused by default
        self._task: asyncio.Task | None = None

        # Row mapping: contact_id (row str) -> row data
        self._contact_map: dict[str, dict] = {}
        # call_sid -> Event set once that call is fully finished
        self._pending: dict[str, asyncio.Event] = {}
        # call_sid -> sheet row, for calls that end without a conversation
        self._call_rows: dict[str, int] = {}

    async def start(self) -> None:
        """Start the campaign in the background."""
        if self.stats.status == "running":
            logger.warning("Campaign %s already running", self.config.campaign_id)
            return

        self.stats.status = "running"
        self.stats.started_at = time.time()
        _active_campaigns[self.config.campaign_id] = self

        self._task = asyncio.create_task(self._run())
        logger.info("Campaign %s started", self.config.campaign_id)

    async def stop(self) -> None:
        """Stop the campaign gracefully."""
        self._stop_event.set()
        self.stats.status = "stopping"
        logger.info("Campaign %s stopping", self.config.campaign_id)

    def pause(self) -> None:
        """Pause the campaign (finish current calls, don't start new ones)."""
        self._pause_event.clear()
        self.stats.status = "paused"
        logger.info("Campaign %s paused", self.config.campaign_id)

    def resume(self) -> None:
        """Resume a paused campaign."""
        self._pause_event.set()
        self.stats.status = "running"
        logger.info("Campaign %s resumed", self.config.campaign_id)

    async def _run(self) -> None:
        """Main campaign loop — fetch contacts and dial them."""
        try:
            # Fetch pending contacts
            contacts = await self.sheets.get_pending_contacts(
                limit=self.config.max_calls or 500,
            )
            self.stats.total_contacts = len(contacts)

            if not contacts:
                logger.info("Campaign %s: no pending contacts", self.config.campaign_id)
                self.stats.status = "completed"
                return

            logger.info(
                "Campaign %s: %d contacts to call",
                self.config.campaign_id, len(contacts),
            )

            # Build contact map for result lookups
            for c in contacts:
                self._contact_map[str(c["row"])] = c

            # Start result processor in background
            result_task = asyncio.create_task(self._process_results())

            # Use semaphore to limit concurrent calls
            semaphore = asyncio.Semaphore(self.config.max_concurrent_calls)

            call_tasks = []
            for contact in contacts:
                if self._stop_event.is_set():
                    break

                # Wait if paused
                await self._pause_event.wait()

                await semaphore.acquire()
                task = asyncio.create_task(
                    self._make_call(contact, semaphore)
                )
                call_tasks.append(task)

                # Pacing delay between starting calls
                await asyncio.sleep(self.config.delay_between_calls)

            # Wait for all calls to finish
            if call_tasks:
                await asyncio.gather(*call_tasks, return_exceptions=True)

            # Signal result processor to stop
            await self._call_results.put(None)
            await result_task

            self.stats.status = "completed"
            self.stats.finished_at = time.time()

        except Exception:
            logger.exception("Campaign %s error", self.config.campaign_id)
            self.stats.status = "error"
            self.stats.errors.append("Campaign loop crashed")
        finally:
            _active_campaigns.pop(self.config.campaign_id, None)
            logger.info(
                "Campaign %s finished: %d calls, %d qualified",
                self.config.campaign_id,
                self.stats.calls_made,
                self.stats.qualified_leads,
            )

    async def _make_call(
        self, contact: dict, semaphore: asyncio.Semaphore
    ) -> None:
        """Initiate a single outbound call."""
        row = contact["row"]
        phone = contact["phone"]
        name = contact["name"]

        if not phone:
            logger.warning("Skipping row %d: no phone number", row)
            semaphore.release()
            return

        try:
            self.stats.active_calls += 1
            contact_id = str(row)

            # Initiate the call via Twilio
            call_sid = initiate_outbound_call(
                to_number=phone,
                campaign_id=self.config.campaign_id,
                contact_id=contact_id,
                agent_config_id=self.config.agent_config_id,
                contact_name=contact.get("name", ""),
                contact_company=contact.get("company", ""),
            )

            if call_sid:
                self.stats.calls_made += 1
                done = self._pending.setdefault(call_sid, asyncio.Event())
                self._call_rows[call_sid] = row
                await self.sheets.mark_calling(row, call_sid)
                logger.info(
                    "Campaign %s: calling %s (%s) sid=%s",
                    self.config.campaign_id, name, phone, call_sid,
                )
                # Hold this concurrency slot until the call is really over,
                # so results are processed before the campaign finishes.
                try:
                    await asyncio.wait_for(done.wait(), timeout=self.config.call_timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Campaign %s: no result for %s after %.0fs",
                        self.config.campaign_id, call_sid, self.config.call_timeout,
                    )
                finally:
                    self._pending.pop(call_sid, None)
                    self._call_rows.pop(call_sid, None)
            else:
                self.stats.calls_failed += 1
                await self.sheets.mark_failed(row, "twilio_initiate_failed")

        except Exception:
            logger.exception("Error calling %s (%s)", name, phone)
            self.stats.calls_failed += 1
            await self.sheets.mark_failed(row, "exception")
        finally:
            self.stats.active_calls -= 1
            semaphore.release()

    async def _process_results(self) -> None:
        """Process call results as they come in from CallSession.on_call_complete."""
        while True:
            result = await self._call_results.get()
            if result is None:
                break

            try:
                contact_id = result.get("contact_id", "")
                contact = self._contact_map.get(contact_id)
                if not contact:
                    logger.warning("Unknown contact_id in result: %s", contact_id)
                    continue

                row = contact["row"]

                # Write results to Google Sheets
                await self.sheets.write_result(row, result)

                # Update stats
                qualification = result.get("qualification") or {}
                if qualification.get("outcome") == "qualified":
                    self.stats.qualified_leads += 1
                if result.get("duration", 0) > 5:
                    self.stats.calls_answered += 1

                logger.info(
                    "Campaign %s: result for row %d — %s",
                    self.config.campaign_id, row,
                    qualification.get("outcome", "unknown"),
                )

            except Exception:
                logger.exception("Error processing call result")
            finally:
                self._release(result.get("call_sid", ""))

    def _release(self, call_sid: str) -> None:
        """Mark a call as finished so its _make_call slot is freed."""
        event = self._pending.get(call_sid)
        if event is not None:
            event.set()

    def on_call_status(self, call_sid: str, status: str) -> None:
        """React to Twilio status callbacks for calls in this campaign."""
        if call_sid not in self._pending:
            return
        status = (status or "").lower()
        if status in TERMINAL_UNANSWERED:
            # Nobody picked up — there will be no conversation result.
            asyncio.create_task(self._finish_without_result(call_sid, status))
        elif status == "completed":
            # Normally the CallSession result follows; don't wait forever if not.
            asyncio.get_running_loop().call_later(
                self.config.result_grace_seconds,
                lambda: asyncio.create_task(
                    self._finish_without_result(call_sid, "no_conversation")
                ),
            )

    async def _finish_without_result(self, call_sid: str, reason: str) -> None:
        event = self._pending.get(call_sid)
        if event is None or event.is_set():
            return  # already finished (e.g. result arrived during the grace period)
        row = self._call_rows.get(call_sid)
        try:
            if row is not None:
                await self.sheets.mark_failed(row, reason)
        except Exception:
            logger.exception("Failed to mark row %s as %s", row, reason)
        finally:
            event.set()

    def get_results_queue(self) -> asyncio.Queue:
        """Get the results queue so CallSession can push results to it."""
        return self._call_results
