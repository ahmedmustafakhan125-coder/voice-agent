"""Read/update the .env file in place and apply changes to live settings.

Used by the setup wizard and dashboard password so changes take effect
immediately (no restart) and persist across restarts.
"""
import re
import threading
from pathlib import Path

_lock = threading.Lock()
_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def env_path() -> Path:
    # Looked up at call time so tests can point it at a temp file.
    from app import config

    return Path(config.ENV_FILE)


def _format_value(value: str) -> str:
    value = str(value)
    if value == "" or re.fullmatch(r"[A-Za-z0-9_\-+.:/@=,]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def update_env_file(updates: dict[str, str], path: Path | None = None) -> Path:
    """Set KEY=value lines, keeping comments, order and unrelated keys intact."""
    path = Path(path) if path else env_path()
    for key, value in updates.items():
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            raise ValueError(f"Invalid env key: {key!r}")
        if "\n" in str(value) or "\r" in str(value):
            raise ValueError(f"Value for {key} must be a single line")

    with _lock:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        remaining = dict(updates)
        out = []
        for line in lines:
            m = _KEY_RE.match(line)
            if m and m.group(1) in remaining:
                key = m.group(1)
                out.append(f"{key}={_format_value(remaining.pop(key))}")
            else:
                out.append(line)
        if remaining:
            if out and out[-1].strip():
                out.append("")
            out.extend(f"{k}={_format_value(v)}" for k, v in remaining.items())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def apply_settings(updates: dict[str, str], persist: bool = True) -> None:
    """Persist KEY=value pairs to .env and update the live settings object."""
    from app.config import settings

    fields = type(settings).model_fields
    typed: dict[str, object] = {}
    for key, value in updates.items():
        name = key.lower()
        if name not in fields:
            raise ValueError(f"Unknown setting: {key}")
        annotation = fields[name].annotation
        typed[name] = annotation(value) if annotation in (int, float) else value

    if persist:
        update_env_file(updates)
    for name, value in typed.items():
        setattr(settings, name, value)
