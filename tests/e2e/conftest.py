"""E2E test fixtures — Playwright browser, dvad server management, seeded data."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
BASELINES = Path(__file__).parent / "baselines"


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


# ─── Local LLM server management ────────────────────────────────────────────

LLAMA_SERVER_BIN = Path("/media/kelleyb/DATA2/LLM/llama.cpp/build/bin/llama-server")
LLAMA_LIB_DIR = LLAMA_SERVER_BIN.parent
LLAMA_MODEL = Path(
    "/media/kelleyb/DATA2/LLM/models/gguf/dolphin-mistral-24b-Q4_K_M.gguf"
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
            "-ngl", "40",
            "-c", "32768",
            "-t", "12",
            "--mlock",
            "--parallel", "1",
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
def e2e_config_path():
    """Path to the E2E-specific models.yaml."""
    path = FIXTURES / "models.yaml"
    if path.exists():
        return path
    return None


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
def dvad_server(seeded_data_dir, e2e_config_path):
    """Start a dvad GUI server for E2E tests, or connect to an external one."""
    url = os.environ.get("DVAD_E2E_URL")
    if url:
        yield url
        return

    port = _find_free_port()
    env = {**os.environ}
    env["DVAD_HOME"] = str(seeded_data_dir)
    if e2e_config_path:
        env["DVAD_E2E_CONFIG"] = str(e2e_config_path)

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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_ready(base_url)
    except TimeoutError:
        proc.terminate()
        proc.wait(timeout=5)
        stdout = proc.stdout.read().decode() if proc.stdout else ""
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        pytest.fail(
            f"dvad server failed to start.\nstdout: {stdout}\nstderr: {stderr}"
        )

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


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
