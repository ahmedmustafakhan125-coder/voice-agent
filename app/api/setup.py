"""Setup wizard API (requires dashboard login).

  GET  /setup/status                 Every integration: configured? masked values
  POST /setup/{integration}          Save values for one integration
  POST /setup/sheets/credentials     Upload the Google service-account JSON
  POST /setup/test/{integration}     Live connection test
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core import integrations
from app.core.env_file import apply_settings
from app.core.integrations import SetupError

router = APIRouter()


class SaveBody(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


class CredentialsBody(BaseModel):
    content: str = Field(..., max_length=20_000)


@router.get("/setup/status")
async def setup_status():
    return integrations.setup_overview()


@router.post("/setup/sheets/credentials")
async def upload_credentials(body: CredentialsBody):
    try:
        email = integrations.save_service_account(body.content)
    except SetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "saved", "service_account_email": email}


@router.post("/setup/test/{integration_id}")
async def test_integration(integration_id: str):
    try:
        return await integrations.run_check(integration_id)
    except SetupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/setup/{integration_id}")
async def save_integration(integration_id: str, body: SaveBody):
    if integration_id not in integrations.BY_ID:
        raise HTTPException(status_code=404, detail="Unknown integration")
    try:
        updates = integrations.prepare_updates(integration_id, body.values)
        if updates:
            apply_settings(updates)
    except SetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "status": "saved",
        "updated": sorted(updates),
        "integration": integrations.integration_status(integrations.BY_ID[integration_id]),
    }
