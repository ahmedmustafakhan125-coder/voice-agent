"""Shared test fixtures.

Tests never touch the real .env, real APIs, or the real data directory.
"""
import os

# Make sure tests never pick up real credentials from a developer's .env
os.environ.setdefault("VOICE_AGENT_ENV_FILE", os.devnull)
