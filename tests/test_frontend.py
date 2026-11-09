"""Static checks on the dashboard page (static/index.html).

These catch the mistakes that otherwise only show up in a browser:
calling an API route that doesn't exist, unsafe HTML injection, broken JS.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML_PATH = Path(__file__).resolve().parent.parent / "static" / "index.html"
HTML = HTML_PATH.read_text(encoding="utf-8")
SCRIPT = re.search(r"<script>(.*)</script>", HTML, re.S).group(1)


def _route_methods(path: str) -> set[str]:
    """HTTP methods the FastAPI app accepts for a concrete path (via its OpenAPI schema)."""
    from app.main import app

    methods = set()
    for template, ops in app.openapi()["paths"].items():
        pattern = "^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template)) + "$"
        if re.match(pattern, path):
            methods |= {m.upper() for m in ops}
    return methods


def _concrete(path: str) -> str:
    path = path.split("?")[0]
    return re.sub(r"\$\{[^}]+\}", "sample", path)


def test_root_serves_dashboard(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "<title>Voice Agent</title>" in res.text


@pytest.mark.parametrize("element_id", [
    "view-overview", "view-calls", "view-campaigns", "view-setup", "view-test", "view-account",
    "form-login", "form-create", "k-total", "daily-chart", "daily-table", "setup-steps",
])
def test_page_has_core_sections(element_id):
    assert f'id="{element_id}"' in HTML


def test_every_api_call_has_a_matching_route_and_method():
    calls = re.findall(r'api\(\s*"(GET|POST|PUT|DELETE)"\s*,\s*[`"]([^`"]+)[`"]', SCRIPT)
    assert len(calls) >= 15, "expected to find the dashboard's API calls"
    for method, path in calls:
        concrete = _concrete(path)
        methods = _route_methods(concrete)
        assert method in methods, f"{method} {path} -> no such route (has {methods or 'none'})"


def test_every_api_path_literal_exists():
    """Paths built separately (e.g. campaign actions) must also exist."""
    paths = {p for p in re.findall(r'[`"](/api/[^`"\s]+)[`"]', SCRIPT)
             if not p.endswith("/")}   # "/api/auth/" is a prefix check, not a call
    assert any("/pause" in p for p in paths)
    for path in paths:
        assert _route_methods(_concrete(path)), f"{path} does not match any route"


def test_no_innerhtml_with_dynamic_data():
    """Names, transcripts and summaries come from sheets/LLM — never parse them as HTML."""
    assignments = re.findall(r"\.innerHTML\s*=\s*([^;]+);", SCRIPT)
    assert assignments == ['ICONS[name] || ""'], assignments
    assert "insertAdjacentHTML" not in SCRIPT
    assert "document.write" not in SCRIPT
    assert "eval(" not in SCRIPT


def test_no_external_scripts_or_styles():
    """The dashboard must work offline / on a private server."""
    assert not re.search(r'<script[^>]+src=["\']https?://', HTML)
    assert not re.search(r'<link[^>]+href=["\']https?://', HTML)


def test_theme_tokens_defined_for_both_modes():
    assert HTML.count("--surface:") >= 3          # light, OS-dark, toggle-dark
    assert ':root[data-theme="dark"]' in HTML
    assert ':root:not([data-theme="light"])' in HTML


def test_daily_chart_has_table_view_and_validated_series_colors():
    assert 'data-mode="table"' in HTML
    for light in ("#2a78d6", "#eb6834", "#1baf7a"):
        assert light in HTML
    for dark in ("#3987e5", "#d95926", "#199e70"):
        assert dark in HTML


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_script_is_valid_javascript(tmp_path):
    js = tmp_path / "dashboard.js"
    js.write_text(SCRIPT, encoding="utf-8")
    result = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
