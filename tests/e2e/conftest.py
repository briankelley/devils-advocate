"""E2E test fixtures -- Playwright browser, dvad server management, seeded data."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
BASELINES = Path(__file__).parent / "baselines"
FAILURES_DIR = Path(__file__).parent / "failures"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

REMOTE_LLM_URL = "https://38.72.121.134/llm"

# Accumulator for findings report (populated by pytest_runtest_makereport)
_collected_results: list[dict] = []
_e2e_ran = False
_session_start: float = 0.0


def pytest_configure(config):
    """Record session start time for duration reporting."""
    global _session_start
    _session_start = time.monotonic()


# ─── Auto-skip unless explicitly opted in ────────────────────────────────────


def pytest_collection_modifyitems(config, items):
    """Auto-skip E2E tests unless explicitly opted in via -m e2e."""
    markexpr = config.option.markexpr or ""
    if "e2e" in markexpr:
        return
    skip = pytest.mark.skip(reason="E2E tests require: -m e2e")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)


# ─── Failure capture + findings accumulator ──────────────────────────────────


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Stash test result on the item and accumulate findings for the report."""
    global _e2e_ran
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)

    # Only collect call-phase results from e2e-marked tests
    if rep.when != "call":
        return
    if "e2e" not in item.keywords and "paranoid" not in item.keywords:
        return

    _e2e_ran = True

    if rep.outcome in ("failed", "skipped") and hasattr(rep, "wasxfail"):
        # xfail: test was expected to fail
        _collected_results.append({
            "nodeid": rep.nodeid,
            "outcome": "xfailed",
            "reason": rep.wasxfail,
            "when": rep.when,
        })
    elif rep.outcome == "failed":
        _collected_results.append({
            "nodeid": rep.nodeid,
            "outcome": "failed",
            "reason": str(rep.longrepr) if rep.longrepr else "",
            "when": rep.when,
        })


# ─── Remote LLM health check ────────────────────────────────────────────────


def _remote_llm_is_healthy() -> bool:
    """Check if the remote LLM endpoint is reachable."""
    try:
        resp = httpx.get(f"{REMOTE_LLM_URL}/health", timeout=10, verify=False)
        return resp.status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError):
        return False


@pytest.fixture(scope="session")
def remote_llm():
    """Ensure remote LLM is reachable. Skips the test session if not."""
    if not _remote_llm_is_healthy():
        pytest.skip(f"Remote LLM at {REMOTE_LLM_URL} is not reachable")
    yield REMOTE_LLM_URL


# ─── SSL bypass for httpx (self-signed cert on remote LLM) ──────────────────


_original_async_client_init = httpx.AsyncClient.__init__


def _patched_async_client_init(self, *args, **kwargs):
    """Force verify=False for all httpx.AsyncClient instances during E2E tests."""
    kwargs.setdefault("verify", False)
    _original_async_client_init(self, *args, **kwargs)


@pytest.fixture(scope="session", autouse=True)
def _disable_ssl_verify():
    """Globally disable SSL verification for httpx during E2E tests."""
    with patch.object(httpx.AsyncClient, "__init__", _patched_async_client_init):
        yield


# ─── Local LLM thinking prompt injection ─────────────────────────────────────

_LOCAL_THINKING_PROMPT = (
    "Think deeply and carefully about the user's request. "
    "Compose your thoughts about the user's prompt between "
    "<think> and </think> tags, then output the final answer "
    "based on your thoughts."
)

_original_async_post = httpx.AsyncClient.post


async def _patched_async_post(self, url, **kwargs):
    """Inject thinking system prompt for local LLM requests."""
    url_str = str(url)
    if "127.0.0.1:8080" in url_str and "chat/completions" in url_str:
        json_body = kwargs.get("json")
        if json_body and isinstance(json_body, dict):
            messages = json_body.get("messages", [])
            # Check if any system message already mentions thinking
            has_thinking_system = any(
                m.get("role") == "system" and "<think>" in (m.get("content") or "")
                for m in messages
            )
            if not has_thinking_system:
                # Prepend thinking instruction as first system message
                messages.insert(0, {"role": "system", "content": _LOCAL_THINKING_PROMPT})
    return await _original_async_post(self, url, **kwargs)


@pytest.fixture(scope="session", autouse=True)
def _inject_local_thinking():
    """Inject thinking system prompt for all local LLM requests during E2E tests."""
    with patch.object(httpx.AsyncClient, "post", _patched_async_post):
        yield


# ─── Local LLM server management (kept for backward compat) ─────────────────

LLAMA_SERVER_BIN = Path("/media/kelleyb/DATA2/LLM/llama.cpp/build/bin/llama-server")
LLAMA_LIB_DIR = LLAMA_SERVER_BIN.parent
LLAMA_MODEL = Path(
    "/media/kelleyb/DATA2/LLM/models/gguf/gemma-3-12b-Thinking.i1-Q3_K_L.gguf"
)
LOCAL_LLM_URL = "http://127.0.0.1:8080"


def _llm_is_running() -> bool:
    """Check if a local LLM server is already responding."""
    try:
        resp = httpx.get(f"{LOCAL_LLM_URL}/v1/models", timeout=5)
        return resp.status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException):
        return False


@pytest.fixture(scope="session")
def local_llm():
    """Ensure a local LLM server is available for live flow tests.

    If one is already running on port 8080, use it.
    Otherwise, launch llama-server as a subprocess and tear it down after tests.
    Skips if the binary or model file is missing.
    """
    if _llm_is_running():
        yield LOCAL_LLM_URL
        return

    if not LLAMA_SERVER_BIN.exists():
        pytest.skip(f"llama-server not found at {LLAMA_SERVER_BIN}")
    if not LLAMA_MODEL.exists():
        pytest.skip(f"LLM model not found at {LLAMA_MODEL}")

    env = {**os.environ, "LD_LIBRARY_PATH": str(LLAMA_LIB_DIR)}
    proc = subprocess.Popen(
        [
            str(LLAMA_SERVER_BIN),
            "-m", str(LLAMA_MODEL),
            "--host", "127.0.0.1",
            "--port", "8080",
            "-ngl", "99",
            "-c", "32768",
            "-t", "12",
            "--mlock",
            "--parallel", "1",
            "--cache-type-k", "q4_0",
            "--cache-type-v", "q4_0",
            "--seed", "42",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        _wait_for_ready(f"{LOCAL_LLM_URL}/v1/models", timeout=60)
    except TimeoutError:
        proc.terminate()
        proc.wait(timeout=10)
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        pytest.fail(f"llama-server failed to start within 60s.\nstderr: {stderr}")

    yield LOCAL_LLM_URL

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@pytest.fixture(scope="session")
def local_llm_available():
    """Check if the local LLM server is reachable. Returns True/False."""
    return _llm_is_running()


# ─── Seeded data directory ───────────────────────────────────────────────────


@pytest.fixture(scope="session")
def seeded_data_dir(tmp_path_factory):
    """Copy captured review into a temp XDG data dir for the E2E server."""
    data_dir = tmp_path_factory.mktemp("dvad_data")
    reviews_dir = data_dir / "reviews"
    reviews_dir.mkdir()
    captured = FIXTURES / "captured_review"
    if captured.exists():
        shutil.copytree(captured, reviews_dir / "captured_e2e_review")
    # Also create logs dir (StorageManager expects it)
    (data_dir / "logs").mkdir(exist_ok=True)
    return data_dir


# ─── E2E config (models.yaml for tests) ─────────────────────────────────────


@pytest.fixture(scope="session")
def e2e_config_path(tmp_path_factory):
    """Copy E2E models.yaml to a temp dir so API mutations don't corrupt the fixture."""
    src = FIXTURES / "models.yaml"
    if not src.exists():
        return None
    tmp = tmp_path_factory.mktemp("dvad_config")
    dst = tmp / "models.yaml"
    shutil.copy2(src, dst)
    return dst


# ─── Server management ───────────────────────────────────────────────────────


def _find_free_port():
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_ready(url: str, timeout: float = 15):
    """Poll the server until it responds or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=2, follow_redirects=True)
            if resp.status_code < 500:
                return
        except (httpx.ConnectError, httpx.TimeoutException):
            pass
        time.sleep(0.3)
    raise TimeoutError(f"Server at {url} did not become ready within {timeout}s")


@pytest.fixture(scope="session")
def dvad_server(seeded_data_dir, e2e_config_path, tmp_path_factory):
    """Start a dvad GUI server for E2E tests, or connect to an external one."""
    url = os.environ.get("DVAD_E2E_URL")
    if url:
        yield url
        return

    port = _find_free_port()
    env = {**os.environ}
    env["DVAD_HOME"] = str(seeded_data_dir)
    env["DVAD_SSL_VERIFY"] = "0"
    env.setdefault("E2E_LOCAL_KEY", "e2e-dummy-key")
    if e2e_config_path:
        env["DVAD_E2E_CONFIG"] = str(e2e_config_path)

    # Use DEVNULL for stdout to prevent pipe buffer deadlock: Rich console
    # output from long-running reviews can fill the 64KB pipe buffer, blocking
    # the server's event loop when no reader drains the pipe.
    server_log = tmp_path_factory.mktemp("dvad_server") / "server.log"
    server_log_fh = open(server_log, "w")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "devils_advocate.gui:create_app_from_env",
            "--factory",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
        ],
        env=env,
        stdout=server_log_fh,
        stderr=subprocess.STDOUT,
    )

    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_ready(base_url)
    except TimeoutError:
        proc.terminate()
        proc.wait(timeout=5)
        server_log_fh.close()
        log_content = server_log.read_text()[-4000:]
        pytest.fail(
            f"dvad server failed to start.\nlog: {log_content}"
        )

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    server_log_fh.close()


# ─── Thinking config toggle ─────────────────────────────────────────────────


@pytest.fixture
def enable_thinking(dvad_server, live_page):
    """Enable thinking on all e2e models, restore after test.

    Original state: e2e-remote=False, e2e-remote-thinker=True.
    This fixture sets both to True, then restores originals on teardown.
    """
    page = live_page
    page.goto(dvad_server)
    page.wait_for_load_state("networkidle")
    csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")

    # Only e2e-remote needs to be changed (e2e-remote-thinker is already True)
    page.request.post(
        f"{dvad_server}/api/config/model-thinking",
        data=json.dumps({"model_name": "e2e-remote", "thinking": True}),
        headers={"X-DVAD-Token": csrf, "Content-Type": "application/json"},
    )
    yield
    # Restore: e2e-remote=False, e2e-remote-thinker=True (original fixture state)
    try:
        page.request.post(
            f"{dvad_server}/api/config/model-thinking",
            data=json.dumps({"model_name": "e2e-remote", "thinking": False}),
            headers={"X-DVAD-Token": csrf, "Content-Type": "application/json"},
            timeout=60_000,
        )
        page.request.post(
            f"{dvad_server}/api/config/model-thinking",
            data=json.dumps({"model_name": "e2e-remote-thinker", "thinking": True}),
            headers={"X-DVAD-Token": csrf, "Content-Type": "application/json"},
            timeout=60_000,
        )
    except Exception:
        pass  # Best-effort; temp config copy protects the fixture file


# ─── Playwright fixtures ────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def browser_context_args():
    """Configure Playwright browser context defaults."""
    return {
        "viewport": {"width": 1280, "height": 720},
        "ignore_https_errors": True,
    }


@pytest.fixture
def live_context(browser):
    """Browser context with video recording for live flow tests."""
    videos_dir = Path(__file__).parent / "videos"
    videos_dir.mkdir(exist_ok=True)
    context = browser.new_context(
        record_video_dir=str(videos_dir),
        record_video_size={"width": 1280, "height": 720},
        viewport={"width": 1280, "height": 720},
    )
    yield context
    context.close()


@pytest.fixture
def live_page(live_context):
    """Page within a video-recording context."""
    page = live_context.new_page()
    yield page
    page.close()


# ─── Auto-generated findings report ─────────────────────────────────────────


def pytest_sessionfinish(session, exitstatus):
    """Write a dated findings report when E2E tests produced xfails or failures."""
    if not _e2e_ran or not _collected_results:
        return

    today = date.today().isoformat()
    report_path = REPO_ROOT / f"e2e-findings-{today}.md"

    # Gather session stats
    passed = failed = xfailed = 0
    for item in session.items:
        rep = getattr(item, "rep_call", None)
        if rep is None:
            continue
        if rep.outcome == "passed":
            passed += 1
        elif rep.outcome == "failed":
            if hasattr(rep, "wasxfail"):
                xfailed += 1
            else:
                failed += 1
        elif rep.outcome == "skipped" and hasattr(rep, "wasxfail"):
            xfailed += 1

    duration = time.monotonic() - _session_start if _session_start > 0 else 0
    duration_str = f"{duration:.0f}s" if duration > 0 else "unknown"

    # Categorize collected results
    failures = [r for r in _collected_results if r["outcome"] == "failed"]
    xfails = [r for r in _collected_results if r["outcome"] == "xfailed"]

    # Sub-categorize xfails by type
    unguarded_xfails = [r for r in xfails if "UNGUARDED DESTRUCTIVE" in r["reason"]]
    policy_xfails = [r for r in xfails if "POLICY VIOLATION" in r["reason"]]
    other_xfails = [r for r in xfails if r not in unguarded_xfails and r not in policy_xfails]

    # Try to import structured metadata for enrichment
    try:
        from tests.e2e.paranoid_helpers import LOSS_ANNOTATIONS, UNGUARDED_DESTRUCTIVE_ENDPOINTS
    except ImportError:
        try:
            from paranoid_helpers import LOSS_ANNOTATIONS, UNGUARDED_DESTRUCTIVE_ENDPOINTS
        except ImportError:
            LOSS_ANNOTATIONS = {}
            UNGUARDED_DESTRUCTIVE_ENDPOINTS = []

    lines = [
        f"# E2E Test Findings - {today}",
        "",
        f"Run: {passed} passed, {xfailed} xfailed, {failed} failed",
        f"Duration: {duration_str}",
        "",
    ]

    # Section: Critical failures
    if failures:
        lines.append("## Critical: Failures (tests that should pass but don't)")
        lines.append("")
        for r in failures:
            test_name = r["nodeid"].split("::")[-1]
            lines.append(f"### {test_name}")
            lines.append(f"- **Test**: `{r['nodeid']}`")
            # Truncate long tracebacks for readability
            reason = r["reason"]
            if len(reason) > 500:
                reason = reason[:500] + "\n  ..."
            lines.append(f"- **Error**:\n  ```\n  {reason}\n  ```")
            lines.append("")

    # Section: Unguarded destructive endpoints
    if unguarded_xfails:
        lines.append("## Findings: Unguarded Destructive Endpoints")
        lines.append("")
        lines.append("These endpoints are irreversible, have no backup, and require no confirmation.")
        lines.append("")
        for r in unguarded_xfails:
            # Extract endpoint key from reason string
            endpoint_key = _extract_endpoint_key(r["reason"])
            lines.append(f"### {endpoint_key or r['nodeid'].split('::')[-1]}")
            if endpoint_key and endpoint_key in LOSS_ANNOTATIONS:
                ann = LOSS_ANNOTATIONS[endpoint_key]
                lines.append(f"- **Endpoint**: `{endpoint_key}`")
                lines.append(f"- **Reversible**: {ann.get('reversible', 'unknown')}")
                lines.append(f"- **Backup**: {ann.get('backup_exists', 'unknown')}")
                lines.append(f"- **Confirmation**: {ann.get('confirmation_required', 'unknown')}")
                lines.append(f"- **On empty input**: {ann.get('on_all_empty', 'unknown')}")
            else:
                lines.append(f"- **Test**: `{r['nodeid']}`")
                lines.append(f"- **Reason**: {r['reason']}")
            lines.append("")

    # Section: Policy violations
    if policy_xfails:
        lines.append("## Findings: Policy Violations")
        lines.append("")
        lines.append("Endpoints where all-empty input causes destructive behavior.")
        lines.append("")
        for r in policy_xfails:
            endpoint_key = _extract_endpoint_key(r["reason"])
            lines.append(f"### {endpoint_key or r['nodeid'].split('::')[-1]}")
            lines.append(f"- **Test**: `{r['nodeid']}`")
            lines.append(f"- **Reason**: {r['reason']}")
            lines.append("")

    # Section: Other xfails
    if other_xfails:
        lines.append("## Findings: Other Expected Failures")
        lines.append("")
        for r in other_xfails:
            test_name = r["nodeid"].split("::")[-1]
            lines.append(f"- **{test_name}**: {r['reason']}")
        lines.append("")

    # Section: Summary table
    if xfails:
        lines.append("## All xfail Summary")
        lines.append("")
        lines.append("| Test | Reason |")
        lines.append("|------|--------|")
        for r in xfails:
            test_name = r["nodeid"].split("::")[-1]
            # Single-line reason for table
            reason_oneline = r["reason"].split("\n")[0].strip()
            lines.append(f"| `{test_name}` | {reason_oneline} |")
        lines.append("")

    # Section: How to use
    lines.append("---")
    lines.append("")
    lines.append("## How to use this file")
    lines.append("")
    lines.append("Feed this document into a new session with the instruction:")
    lines.append("")
    lines.append("> Fix the paranoid E2E findings documented in")
    lines.append(f"> `e2e-findings-{today}.md`. Start with the critical findings.")
    lines.append("> After each fix, convert the corresponding xfail test in")
    lines.append("> `tests/e2e/test_paranoid_loss_annotations.py` to a real assertion,")
    lines.append("> then run the paranoid suite to verify:")
    lines.append("> `.venv/bin/python -m pytest tests/e2e/test_paranoid_*.py -m e2e -v`")
    lines.append("")

    report_path.write_text("\n".join(lines))


def _extract_endpoint_key(reason: str) -> str | None:
    """Extract an endpoint key like 'POST /api/config' from an xfail reason string."""
    for method in ("GET", "POST", "PUT", "DELETE", "PATCH"):
        prefix = f"{method} /api/"
        idx = reason.find(prefix)
        if idx != -1:
            # Grab from the method to the next newline or end
            rest = reason[idx:]
            end = len(rest)
            for stop in ("\n", "  ", "\t"):
                pos = rest.find(stop)
                if pos != -1 and pos < end:
                    end = pos
            return rest[:end].strip()
    return None
