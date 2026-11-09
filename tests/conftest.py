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

from app.storage.call_log import CallLog, set_call_log  # noqa: E402


@pytest.fixture
def call_log():
    """A fresh in-memory call log installed as the process-wide instance."""
    log = CallLog(":memory:")
    set_call_log(log)
    yield log
    set_call_log(None)
    log.close()
