import os

from pydantic_settings import BaseSettings

# Which env file to load. Overridable so tests (and the setup wizard) can
# point at a different file without touching the real .env.
ENV_FILE = os.environ.get("VOICE_AGENT_ENV_FILE", ".env")


class Settings(BaseSettings):
    # Twilio (client provides these)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

    # Deepgram STT
    deepgram_api_key: str = ""

    # Google Gemini LLM
    gemini_api_key: str = ""

    # ElevenLabs TTS
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"  # default Rachel voice

    # Google Sheets
    google_sheets_credentials_file: str = "credentials/service-account.json"
    google_sheet_id: str = ""

    # Email
    sendgrid_api_key: str = ""
    sendgrid_from_email: str = "noreply@yourdomain.com"

    # Server
    public_url: str = "https://your-domain.com"
    host: str = "0.0.0.0"
    port: int = 8000

    # Local data (call history database)
    data_dir: str = "data"

    model_config = {"env_file": ENV_FILE, "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
