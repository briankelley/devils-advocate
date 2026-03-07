# E2E Test Harness Audit & Rewire Plan

## Problem Statement

The test harness has accumulated structural debt through iterative feature work.
The `run-tests` shell script has three tiers (`unit`, `e2elocal`, `e2eremote`)
but `e2eremote` was never correctly wired — it runs the wrong tests against the
wrong backend. The `.134` server (RTX 4000 SFF Ada) infrastructure exists in
conftest but is unused.

## Current State (as of 2026-03-06)

### Marker Map

| Marker | Location | Purpose |
|---|---|---|
| _(none)_ | `tests/test_*.py` | Unit tests (mocked, no LLM, no browser) |
| `live` | `tests/test_e2e_live.py` | Direct orchestrator calls using real `models.yaml` — **hits whatever providers are configured, potentially cloud** |
| `e2e` | `tests/e2e/test_dashboard.py`, `test_config_page.py`, `test_visual_regression.py`, `test_review_detail.py`, `test_matrix.py` (base) | Playwright GUI tests, no LLM needed |
| `e2e` + `e2e_live` | `tests/e2e/test_review_flow.py`, `test_matrix.py` (3 live classes), `test_max_out_enforcement.py` | Playwright GUI + local llama-server |
| `e2e` + `paranoid` | `tests/e2e/test_paranoid_*.py` | Paranoid audit (destructive path coverage) |

### run-tests Routing (Current)

| Tier | Stages | What actually happens |
|---|---|---|
| `unit` | `tests/ -m "not e2e and not live and not paranoid" --ignore=tests/e2e` | Correct |
| `e2elocal` | unit → gui → start_llama → `tests/e2e/ -m "e2e_live"` → paranoid | Correct but never been run |
| `e2eremote` | unit → gui → `tests/ -m "live" --ignore=tests/e2e` → paranoid | **WRONG** — runs `test_e2e_live.py` (non-Playwright, hits models.yaml providers) instead of Playwright tests against .134 |

### Unused Infrastructure

- `REMOTE_LLM_URL = "https://38.72.121.134/llm"` — defined in `tests/e2e/conftest.py:24`
- `remote_llm()` fixture — defined, health-checked, SSL-bypassed, **never consumed**
- `_remote_llm_is_healthy()` — working health check, never used by tests

### conftest Import Collision

Unit tests do `from conftest import make_model_config` (bare Python import).
When pytest collects from both `tests/` and `tests/e2e/`, Python resolves to
`tests/e2e/conftest.py` instead of `tests/conftest.py`. The `--ignore=tests/e2e`
on the unit and remote stages is a bandaid for this. Root cause: bare `from
conftest import` in 9 unit test files.

### Fixes Already Applied This Session

1. Removed all `model-thinking` references from paranoid tests + helpers + conftest
   (endpoint was removed in ff70777, tests weren't updated)
2. Rewrote `enable_thinking` fixture to use structured `POST /api/config` payload
3. Deleted stale visual regression baselines (will regenerate on next run)
4. Fixed `parse_summary` — strips ANSI codes, captures full summary line
5. Fixed `append_section` — captures ERRORS and short test summary blocks, not just FAILURES
6. Consolidated paranoid report section to use `append_section`
7. Added `--ignore=tests/e2e` to e2eremote stage (bandaid for conftest collision)

## Audit Plan

### Phase 1: Inventory & Structure (read-only)

1. **Catalog every test file** — marker, fixture deps, what it actually tests
2. **Map fixture dependency graph** — which conftest provides what, who consumes it
3. **Identify the 9 unit test files** doing `from conftest import` and list what they import
4. **Check e2e fixture YAML** — `tests/e2e/fixtures/models.yaml` — what models/endpoints
5. **Check `test_e2e_live.py`** — what does `load_config()` resolve to, what providers get hit
6. **Verify llama-server binary and model** exist at hardcoded paths

### Phase 2: Fix conftest Imports (eliminate --ignore bandaid)

The 9 unit test files that do `from conftest import ...`:
- `test_common.py` — `make_author_final`, etc.
- `test_cost.py` — `make_model_config`
- `test_dedup.py` — `make_model_config`, `make_review_point`
- `test_formatting.py` — `make_author_response`, etc.
- `test_governance.py` — `make_author_final`, etc.
- `test_ids.py` — `make_review_group`, `make_review_point`
- `test_output.py` — `make_author_final`, etc.
- `test_parser.py` — `make_review_group`, `make_review_point`
- `test_revision.py` — `make_review_group`, `make_review_point`

**Fix:** These are factory helpers, not pytest fixtures. Move them to
`tests/helpers.py` (or similar) and change imports to `from helpers import ...`.
This eliminates the conftest shadowing entirely. Remove `--ignore=tests/e2e`
from run-tests after this is done.

### Phase 3: Rewire e2eremote

**Goal:** `e2eremote` runs the SAME Playwright `e2e_live` tests as `e2elocal`,
but against the .134 server instead of localhost llama-server.

Options:
- **A) Environment variable:** `DVAD_LLM_URL` env var read by conftest. `e2elocal`
  sets it to `http://127.0.0.1:8080`, `e2eremote` sets it to `https://38.72.121.134/llm`.
  The `local_llm` fixture checks this var and skips launching llama-server when remote.
- **B) Separate marker:** Add `e2e_remote` marker, duplicate test classes. Wasteful.
- **C) Fixture parameterization:** Parametrize the LLM URL fixture. Complex.

**Recommendation: Option A.** Minimal change, no test duplication.

Changes needed:
1. Update `tests/e2e/conftest.py`:
   - `local_llm` fixture reads `DVAD_LLM_URL` env var
   - If set to remote URL, health-check that instead of starting llama-server
   - If unset or `http://127.0.0.1:8080`, current behavior (start llama if needed)
2. Update `run-tests`:
   - `e2elocal` stage: no change (or explicitly `export DVAD_LLM_URL=http://127.0.0.1:8080`)
   - `e2eremote` stage: `export DVAD_LLM_URL=https://38.72.121.134/llm`, then run
     `tests/e2e/ -m "e2e_live"` (same as e2elocal, different URL)
   - Remove the current `tests/ -m "live"` remote stage entirely
3. Decide what to do with `tests/test_e2e_live.py` (the `@pytest.mark.live` tests):
   - These are integration tests calling orchestrators directly
   - They use `load_config()` (real models.yaml) — wallet risk if cloud models configured
   - Either: wire them to use the .134 server too, or keep as a separate opt-in tier

### Phase 4: Cleanup

1. Remove `remote_llm` fixture if replaced by env-var approach
2. Update `run-tests` help text to accurately describe what each tier does
3. Remove `--ignore=tests/e2e` if Phase 2 is complete
4. Audit `test_e2e_live.py` — decide: keep, remove, or rewire to self-hosted only
5. Verify all three tiers work end-to-end: `unit`, `e2elocal`, `e2eremote`

## Files to Touch

| File | Phase | Change |
|---|---|---|
| `tests/test_common.py` | 2 | `from conftest import` → `from helpers import` |
| `tests/test_cost.py` | 2 | same |
| `tests/test_dedup.py` | 2 | same |
| `tests/test_formatting.py` | 2 | same |
| `tests/test_governance.py` | 2 | same |
| `tests/test_ids.py` | 2 | same |
| `tests/test_output.py` | 2 | same |
| `tests/test_parser.py` | 2 | same |
| `tests/test_revision.py` | 2 | same |
| `tests/helpers.py` | 2 | NEW — extract factory helpers from conftest |
| `tests/conftest.py` | 2 | Remove factory helpers (keep fixtures + live gate) |
| `tests/e2e/conftest.py` | 3 | Read `DVAD_LLM_URL`, adapt `local_llm` fixture |
| `run-tests` | 3,4 | Rewire e2eremote, remove --ignore, update help |
| `tests/test_e2e_live.py` | 4 | Audit — decide fate |
