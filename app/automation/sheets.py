"""Google Sheets integration for reading contacts and writing call results.

Expected sheet layout (first row = headers):
  A: Name | B: Phone | C: Company | D: Status | E: Call SID |
  F: Duration | G: Interest | H: Email | I: Outcome | J: Summary | K: Called At

Status values: pending, calling, completed, failed, do_not_call
"""
import asyncio
import logging
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

from app.config import settings

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Column mapping (1-indexed)
COL_NAME = 1
COL_PHONE = 2
COL_COMPANY = 3
COL_STATUS = 4
COL_CALL_SID = 5
COL_DURATION = 6
COL_INTEREST = 7
COL_EMAIL = 8
COL_OUTCOME = 9
COL_SUMMARY = 10
COL_CALLED_AT = 11

EXPECTED_HEADERS = [
    "Name", "Phone", "Company", "Status", "Call SID",
    "Duration", "Interest", "Email", "Outcome", "Summary", "Called At",
]


class SheetsClient:
    """Read contacts from and write results to Google Sheets."""

    def __init__(self):
        self._client: gspread.Client | None = None
        self._sheet: gspread.Spreadsheet | None = None
        self._worksheet: gspread.Worksheet | None = None

    def _connect(self) -> None:
        """Authenticate and open the configured spreadsheet."""
        if self._client is not None:
            return

        creds = Credentials.from_service_account_file(
            settings.google_sheets_credentials_file,
            scopes=SCOPES,
        )
        self._client = gspread.authorize(creds)
        self._sheet = self._client.open_by_key(settings.google_sheet_id)
        self._worksheet = self._sheet.sheet1
        logger.info("Connected to Google Sheet: %s", self._sheet.title)

    def _ensure_headers(self) -> None:
        """Create headers if the sheet is empty."""
        self._connect()
        first_row = self._worksheet.row_values(1)
        if not first_row or first_row[0] != "Name":
            self._worksheet.update("A1:K1", [EXPECTED_HEADERS])
            logger.info("Created headers in Google Sheet")

    async def get_pending_contacts(self, limit: int = 50) -> list[dict]:
        """Get contacts with status 'pending' (not yet called).

        Returns list of dicts with: row, name, phone, company
        """
        def _fetch():
            self._connect()
            self._ensure_headers()
            all_rows = self._worksheet.get_all_values()
            contacts = []

            for i, row in enumerate(all_rows[1:], start=2):  # skip header
                if len(row) < COL_STATUS:
                    continue

                status = row[COL_STATUS - 1].strip().lower() if len(row) >= COL_STATUS else ""
                if status in ("", "pending"):
                    contacts.append({
                        "row": i,
                        "name": row[COL_NAME - 1].strip() if len(row) >= COL_NAME else "",
                        "phone": row[COL_PHONE - 1].strip() if len(row) >= COL_PHONE else "",
                        "company": row[COL_COMPANY - 1].strip() if len(row) >= COL_COMPANY else "",
                    })
                    if len(contacts) >= limit:
                        break

            return contacts

        return await asyncio.to_thread(_fetch)

    async def mark_calling(self, row: int, call_sid: str) -> None:
        """Mark a contact as currently being called."""
        def _update():
            self._connect()
            self._worksheet.update_cell(row, COL_STATUS, "calling")
            self._worksheet.update_cell(row, COL_CALL_SID, call_sid)
            self._worksheet.update_cell(
                row, COL_CALLED_AT,
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            )

        await asyncio.to_thread(_update)
        logger.info("Marked row %d as calling (sid=%s)", row, call_sid)

    async def write_result(self, row: int, result: dict) -> None:
        """Write call results back to the sheet.

        Args:
            row: The sheet row number for this contact.
            result: Dict with keys from qualification output.
        """
        def _update():
            self._connect()
            qualification = result.get("qualification") or {}

            self._worksheet.update_cell(row, COL_STATUS, "completed")
            self._worksheet.update_cell(
                row, COL_DURATION,
                f"{result.get('duration', 0):.0f}s",
            )
            self._worksheet.update_cell(
                row, COL_INTEREST,
                qualification.get("interest_level", "unknown"),
            )
            self._worksheet.update_cell(
                row, COL_EMAIL,
                qualification.get("email_address") or "",
            )
            self._worksheet.update_cell(
                row, COL_OUTCOME,
                qualification.get("outcome", "unknown"),
            )
            self._worksheet.update_cell(
                row, COL_SUMMARY,
                qualification.get("summary", ""),
            )

        await asyncio.to_thread(_update)
        logger.info("Wrote results for row %d", row)

    async def mark_failed(self, row: int, reason: str = "call_failed") -> None:
        """Mark a contact as failed."""
        def _update():
            self._connect()
            self._worksheet.update_cell(row, COL_STATUS, "failed")
            self._worksheet.update_cell(row, COL_SUMMARY, reason)

        await asyncio.to_thread(_update)
        logger.info("Marked row %d as failed: %s", row, reason)

    async def get_contact_by_id(self, contact_id: str) -> dict | None:
        """Get a contact by row number (contact_id = row number as string)."""
        try:
            row = int(contact_id)
        except (ValueError, TypeError):
            return None

        def _fetch():
            self._connect()
            values = self._worksheet.row_values(row)
            if not values:
                return None
            return {
                "row": row,
                "name": values[COL_NAME - 1] if len(values) >= COL_NAME else "",
                "phone": values[COL_PHONE - 1] if len(values) >= COL_PHONE else "",
                "company": values[COL_COMPANY - 1] if len(values) >= COL_COMPANY else "",
            }

        return await asyncio.to_thread(_fetch)
