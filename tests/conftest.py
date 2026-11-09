"""Shared test fixtures.

Tests never touch the real .env, real APIs, or the real data directory.
"""
import os
import tempfile

# Must run before any `app.*` import so Settings picks these up.
# Never read real credentials from a developer's .env:
os.environ["VOICE_AGENT_ENV_FILE"] = os.devnull
# Never write into the project's real data/ folder:
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="voice_agent_test_")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.storage.call_log import CallLog, set_call_log  # noqa: E402

LOCAL = ("127.0.0.1", 50000)
REMOTE = ("203.0.113.7", 50000)   # TEST-NET address = "someone on the internet"


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Each test gets its own .env file and starts from pristine settings."""
    from app import config
    from app.config import settings
    from app.core import auth

    env_file = tmp_path / ".env"
    monkeypatch.setattr(config, "ENV_FILE", str(env_file))
    snapshot = settings.model_dump()
    yield env_file
    for key, value in snapshot.items():
        setattr(settings, key, value)
    auth.login_limiter.reset()


@pytest.fixture
def call_log():
    """A fresh in-memory call log installed as the process-wide instance."""
    log = CallLog(":memory:")
    set_call_log(log)
    yield log
    set_call_log(None)
    log.close()


@pytest.fixture
def client(call_log):
    """Browser on the server itself (no password needed before one is set)."""
    from app.main import app

    return TestClient(app, client=LOCAL)


@pytest.fixture
def remote_client(call_log):
    """Browser somewhere on the internet."""
    from app.main import app

    return TestClient(app, client=REMOTE)
