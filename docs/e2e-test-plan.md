# E2E Testing Harness Plan — Devil's Advocate GUI

## Overview

Playwright-based visual regression + interaction testing for the dvad web GUI.
Locks in the current approved state of every screen and interaction path, then
validates future changes against those baselines.

---

## 1. Dependencies & Project Setup

### New dev dependencies (add to `pyproject.toml [project.optional-dependencies]`)

```toml
e2e = [
    "pytest-playwright>=0.5",
    "playwright>=1.48",
    "Pillow>=10.0",          # screenshot diff tooling (pixelmatch uses it)
    "pixelmatch>=0.3",       # perceptual diff engine
]
```

### Install steps

```bash
cd /media/kelleyb/DATA2/code/tools/devils-advocate
pip install -e ".[dev,e2e]"
playwright install chromium       # single browser, keeps it fast
```

### Directory layout

```
tests/
├── e2e/
│   ├── conftest.py              # Playwright fixtures, server management
│   ├── test_dashboard.py        # Dashboard screen states
│   ├── test_review_flow.py      # Full review lifecycle (SSE, details, resolution)
│   ├── test_config_page.py      # Config editor screens
│   ├── test_visual_regression.py# Screenshot capture + diff orchestrator
│   ├── baselines/               # Golden screenshots (git-tracked)
│   │   ├── dashboard_empty.png
│   │   ├── dashboard_with_reviews.png
│   │   ├── review_running.png
│   │   ├── review_complete.png
│   │   ├── review_escalated.png
│   │   ├── config_structured.png
│   │   ├── config_raw_yaml.png
│   │   └── ...
│   ├── diffs/                   # Generated diff images (gitignored)
│   ├── videos/                  # Flight recorder videos (gitignored)
│   └── fixtures/
│       └── captured_review/     # Snapshot of a real completed review directory
│           ├── review-ledger.json
│           ├── round1/
│           ├── round2/
│           ├── dvad-report.md
│           └── original_content.txt
└── conftest.py                  # (existing — untouched)
```

### pytest marker

Add to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
markers = [
    "live: e2e tests that make real API calls (select with '-m live')",
    "e2e: end-to-end Playwright GUI tests (select with '-m e2e')",
]
```

E2E tests are **excluded by default** (same gating pattern as `live`).
Run with `pytest -m e2e` or `pytest tests/e2e/`.

---

## 2. Server Management (`tests/e2e/conftest.py`)

### Dual-mode fixture

```python
# If DVAD_E2E_URL is set → connect to that running instance
# Otherwise → start dvad via uvicorn in a subprocess, wait for ready, tear down after

@pytest.fixture(scope="session")
def dvad_server():
    url = os.environ.get("DVAD_E2E_URL")
    if url:
        yield url   # external instance
        return

    # Start subprocess
    port = _find_free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "devils_advocate.gui:create_app",
         "--factory", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        env={**os.environ, "DVAD_E2E_CONFIG": str(e2e_config_path)},
    )
    _wait_for_ready(f"http://127.0.0.1:{port}", timeout=15)
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(timeout=5)
```

### E2E-specific config

A dedicated `models.yaml` for E2E tests that points all roles at the local LLM
simulation endpoint (see §6). Stored at `tests/e2e/fixtures/models.yaml`.

Config resolution: the fixture-managed server passes the E2E config via a new
env var `DVAD_E2E_CONFIG`. The `create_app` factory already accepts
`config_path` — the uvicorn `--factory` call will need a thin wrapper to
read this env var and pass it through. This is the **one code change** to
dvad itself:

```python
# src/devils_advocate/gui/__init__.py  — add factory variant
def create_app_from_env():
    """Factory for uvicorn --factory that reads config from env."""
    config_path = os.environ.get("DVAD_E2E_CONFIG") or None
    return create_app(config_path=config_path)
```

---

## 3. Pre-Seeded Review Fixture

### Capture process (one-time, manual)

1. Run a real review with the current production config:
   ```bash
   dvad review --mode plan --project e2e-fixture-capture --input <sample_file>
   ```
2. Copy the resulting review directory from
   `~/.local/share/devils-advocate/reviews/<review_id>/` into
   `tests/e2e/fixtures/captured_review/`
3. The fixture loads this into a temp data directory before the server starts,
   giving the dashboard and detail pages real data to render.

### Fixture mechanics

```python
@pytest.fixture(scope="session")
def seeded_data_dir(tmp_path_factory):
    """Copy captured review into a temp XDG data dir."""
    data_dir = tmp_path_factory.mktemp("dvad_data")
    reviews_dir = data_dir / "reviews"
    shutil.copytree(FIXTURES / "captured_review", reviews_dir / "captured_001")
    return data_dir
```

The E2E server is started with `DVAD_HOME=<seeded_data_dir>` so it discovers
the pre-seeded review on the dashboard.

---

## 4. Visual Regression Strategy

### Screenshot capture

Each test captures named screenshots at key screen states:

```python
async def test_dashboard_with_reviews(page, dvad_server):
    await page.goto(f"{dvad_server}/")
    await page.wait_for_selector(".review-table")
    await page.screenshot(path=BASELINES / "dashboard_with_reviews.png", full_page=True)
```

### Diff strategy — configurable

**Default: perceptual with threshold** via Playwright's built-in
`expect(page).to_have_screenshot()` which uses pixelmatch internally:

```python
await expect(page).to_have_screenshot(
    name="dashboard_with_reviews.png",
    max_diff_pixel_ratio=0.002,   # 0.2% tolerance
    threshold=0.2,                 # per-pixel color distance threshold
)
```

**Strict mode** via env var `DVAD_E2E_PIXEL_PERFECT=1`:

```python
if os.environ.get("DVAD_E2E_PIXEL_PERFECT"):
    threshold, max_diff = 0.0, 0
else:
    threshold, max_diff = 0.2, 0.002
```

### Video recording (flight recorder)

Live flow tests record the browser viewport as video. Disabled for static tests
(nothing interesting to watch). Videos are diagnostic artifacts — they don't
assert anything, but they're invaluable for debugging temporal issues like
duplicate SSE messages, flickering UI state, or out-of-order event rendering.

```python
# In conftest.py — live flow tests get video, static tests don't
@pytest.fixture
def live_context(browser):
    context = browser.new_context(
        record_video_dir="tests/e2e/videos/",
        record_video_size={"width": 1280, "height": 720},
    )
    yield context
    context.close()  # video is saved on close
```

- Videos saved to `tests/e2e/videos/` (gitignored)
- Retained on failure, auto-cleaned on pass (configurable)
- 1280×720 keeps file sizes small (~2-5MB per review cycle)

### Failure output

- Playwright auto-generates diff images in `test-results/` showing
  expected vs actual vs highlighted-diff
- Video recordings from live flow tests show the full timeline of events

### Baseline update workflow

When an intentional UI change lands:

```bash
pytest -m e2e --update-snapshots    # Playwright built-in flag
git add tests/e2e/baselines/
git commit -m "Update E2E visual baselines for <change>"
```

---

## 5. Scripted E2E Interaction Paths

### Test Suite Structure

#### `test_dashboard.py`
| Test | What it validates |
|------|-------------------|
| `test_dashboard_loads` | Page title, nav bar, review table or empty state |
| `test_dashboard_with_seeded_review` | Captured review appears in table with correct mode badge, date, status |
| `test_dashboard_new_review_form` | Mode selector, file picker modal opens, project field present |
| `test_dashboard_pagination` | If >10 reviews, pagination controls work |
| `test_dashboard_visual` | Screenshot baseline of dashboard with data |

#### `test_review_flow.py`
| Test | What it validates |
|------|-------------------|
| `test_initiate_review` | Submit new review via form → redirects to progress page → SSE connects |
| `test_sse_populates_log` | SSE events appear in log panel (phase dots update, cost table fills) |
| `test_review_completes` | Terminal `complete` event → page transitions to results view |
| `test_detail_page_groups` | Escalated/accepted/dismissed groups render with correct badges |
| `test_override_escalated` | Click override button → modal → confirm → group status changes |
| `test_download_report` | "Download report" link returns dvad-report.md |
| `test_download_revised` | "Download revised" link works (if revision was generated) |
| `test_review_running_visual` | Screenshot of SSE progress view mid-stream |
| `test_review_complete_visual` | Screenshot of completed results |

#### `test_config_page.py`
| Test | What it validates |
|------|-------------------|
| `test_config_loads` | Structured tab renders model cards with role badges |
| `test_config_raw_tab` | Switch to raw YAML tab, editor renders |
| `test_config_validate` | Edit YAML, click validate → shows valid/issues |
| `test_config_visual` | Screenshot baselines for both tabs |

### SSE handling pattern

```python
async def test_sse_populates_log(page, dvad_server):
    # Start a review (will use local LLM endpoint)
    review_id = await _start_review(page, dvad_server)

    # Wait for SSE content to appear
    await page.wait_for_selector(".log-line", timeout=60_000)

    # Wait for phase progression
    await page.wait_for_function(
        "document.querySelectorAll('.phase-dot.active').length >= 2",
        timeout=120_000,
    )

    # Verify cost table has at least one entry
    cost_rows = await page.locator(".cost-row").count()
    assert cost_rows >= 1
```

### Timeout strategy

- SSE tests with local LLM: **120s timeout** (8B model on 4090 is fast but
  the full 2-round protocol takes multiple calls)
- Pre-seeded data tests: **10s timeout** (just rendering)
- Config page tests: **10s timeout**

---

## 6. Local LLM Simulation Endpoint

### Architecture

A dedicated `models.yaml` for E2E tests that routes all roles to a single local
model via the existing OpenAI-compatible provider path. **Zero code changes to
dvad's provider layer.**

```yaml
# tests/e2e/fixtures/models.yaml
models:
  e2e-local:
    provider: openai
    model_id: dolphin-mistral-24b-Q4_K_M.gguf
    api_key_env: E2E_LOCAL_KEY         # set to "none" — llama.cpp doesn't check
    api_base: http://localhost:8080/v1  # llama-server default port
    context_window: 32768
    timeout: 300
    max_out_stated: 8192
    max_out_configured: 4096
    thinking: false                     # avoids thinking-token edge cases

roles:
  author: e2e-local
  reviewers:
    - e2e-local
    - e2e-local
  deduplication: e2e-local

settings:
  live_testing: false
```

### llama.cpp execution engine

Local LLM inference via `llama-server` (from llama.cpp). GGUF models stored at
`/media/kelleyb/DATA2/LLM/models/gguf/`.

**Start the server before running live flow tests:**

```bash
llama-server \
  -m /media/kelleyb/DATA2/LLM/models/gguf/dolphin-mistral-24b-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8080 \
  -ngl 40 \                # all layers to GPU (40/41, output layer auto-offloaded)
  -c 32768 \               # context window (match models.yaml)
  --parallel 1 \           # single KV slot — frees ~3.7GB VRAM, 36% faster wall-clock
  --mlock                  # pin model in RAM, prevent swapping
```

Key notes:
- `llama-server` exposes `/v1/chat/completions` — the exact endpoint dvad's
  `call_openai_compatible()` already calls. No shim or adapter needed.
- Single model serves all roles sequentially (`--parallel 1`). The 2-round
  adversarial protocol makes ~6-8 LLM calls per review. On a 4090 with the
  24B Q4 model this completes in roughly 4-8 minutes depending on input size.
- `-ngl 40` offloads all 40 transformer layers + output to GPU. `--parallel 1`
  keeps the KV cache to a single 32k slot (~1.3GB vs ~5GB at parallel 4).
  Stress-tested: monster payload (25.7k tokens) completes in 72s vs 113s
  with the old 4-slot config.

### Thinking/structured output considerations

- Set `thinking: false` — avoids the thinking-token wrapping code paths
  (Anthropic adaptive thinking, OpenAI reasoning_effort) that smaller models
  can't produce correctly.
- The local model just needs to return valid XML/JSON in the format dvad's
  parser expects. If the model's output is malformed, dvad's normalization
  layer (`normalization.py`) already handles fallback parsing — this is
  exercised for free.
- If structured output compliance proves problematic with the chosen model,
  a thin **response-shaping proxy** can sit between dvad and the local model
  server. This proxy would: (a) pass through the request, (b) post-process the
  response to fix common structural issues (unclosed XML tags, etc.).
  **This is a fallback — try without it first.**

### Server startup

The llama-server instance is **not managed by the test harness** — it runs
independently (start it in a tmux pane or systemd unit). The E2E test suite
checks connectivity at session start and skips with a clear message if
unreachable:

```python
@pytest.fixture(scope="session", autouse=True)
def _check_local_llm():
    """Skip E2E review-flow tests if local LLM is not reachable."""
    if os.environ.get("DVAD_E2E_URL"):
        return  # external server mode, LLM config is their problem
    try:
        httpx.get("http://localhost:8080/v1/models", timeout=5)
    except httpx.ConnectError:
        pytest.skip("Local LLM server (llama-server) not running on :8080")
```

### Two test tiers

| Tier | Needs local LLM? | What it tests |
|------|-------------------|---------------|
| **Static** | No | Dashboard, detail page, config — all from pre-seeded data |
| **Live flow** | Yes | Initiate review → SSE → completion → resolution |

Static tests run fast (~10s) with no GPU. Live flow tests need the local model
server but avoid all commercial API costs.

---

## 7. Integration with Existing Test Suite

### Isolation

- E2E tests live in `tests/e2e/` — separate from the 734+ unit/integration tests
- Marked with `@pytest.mark.e2e` and **excluded from the default test run**
- Never imported by or import from existing test modules
- Own `conftest.py` — does not modify the shared `tests/conftest.py`

### Run commands

```bash
# Existing suite (unchanged)
pytest                                    # runs 734+ tests in ~2min

# E2E static tests only (no LLM needed)
pytest -m "e2e and not e2e_live"          # ~30s

# Full E2E including review flow (needs llama-server on :8080)
pytest -m e2e                             # ~3-5min

# E2E against running server
DVAD_E2E_URL=http://localhost:8411 pytest -m e2e

# Update visual baselines
pytest -m e2e --update-snapshots
```

### Marker sub-classification

```python
# tests/e2e/conftest.py
def pytest_collection_modifyitems(config, items):
    """Auto-skip E2E tests unless explicitly opted in."""
    if "e2e" in (config.option.markexpr or ""):
        return
    skip = pytest.mark.skip(reason="E2E tests require: -m e2e")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)
```

---

## 8. Implementation Order

| Phase | Work | Estimated scope |
|-------|------|-----------------|
| **A** | Scaffold: directory structure, conftest.py, pyproject.toml deps, E2E config, `create_app_from_env` wrapper | ~150 lines |
| **B** | Capture real review fixture, seed it into the test data dir | Manual + ~50 lines fixture code |
| **C** | Static tests: dashboard, detail page, config page (pre-seeded data only) | ~300 lines |
| **D** | Visual regression: screenshot capture, diff config, baseline generation | ~150 lines |
| **E** | Local LLM config + live flow tests: initiate review, SSE, resolution | ~400 lines |
| **F** | Polish: CI integration notes, README, marker docs | ~50 lines |

Total new code: **~1,100 lines** across ~6 files.
Single code change to dvad itself: the `create_app_from_env` factory (~5 lines).

---

## 9. Local CI Automation (Optional Future Step)

All testing runs locally on the development workstation. A lightweight local CI
loop can be added later to auto-trigger the E2E suite on git events:

```bash
# Example: git post-commit hook or inotifywait watcher
pytest -m "e2e and not e2e_live" --tb=short   # static tier, ~30s, no GPU
pytest -m e2e --tb=short                        # full tier if llama-server is up
```

This is purely local automation — no remote CI service needed. The 4090 handles
both the llama-server and Playwright concurrently without contention since
Playwright uses CPU/RAM and the model uses GPU/VRAM.

---

## 10. Open Decisions (User)

1. **Model selection** — which GGUF model to load on the 4090 for E2E testing.
   Plan doesn't prescribe this. Priority: structured output compliance (valid
   XML/JSON) over raw quality. Instruction-tuned models recommended.
2. **llama-server chat template** — depends on the chosen model (chatml, llama3,
   mistral, etc.). Set via `--chat-template` flag.
