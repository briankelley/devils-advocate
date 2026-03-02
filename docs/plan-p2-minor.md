# P2 Implementation Plan: Minor Fixes and Enhancements

## 3. Auto-init config on `dvad gui` when no config exists

**Problem:** `dvad gui` errors out when no models.yaml exists. It tells the user to go to /config to fix it, but could just scaffold the config automatically.

**Files:** `src/devils_advocate/gui/app.py` or `src/devils_advocate/gui/api.py` (the startup config load path)

**Fix:** Before erroring on missing config, call `init_config()` silently. The GUI already has a config editor at /config, so the user can configure models from there.

---

## 4. Latency message near progress bar + fix time estimate formula

**Problem:** The "expect ~17 min" estimate was off by 3.5x on spec review (actual: 4m47s) and meaningless on code review. No general guidance that reviews take time.

**Files:**
- `src/devils_advocate/revision.py` or wherever the estimate is calculated (grep for "expect")
- `src/devils_advocate/gui/templates/review_detail.html` -- add a static note near the progress bar

**Fix:**
- Fix the estimate formula (likely assumes wrong tokens-per-second rate)
- Add a brief note near the progress indicator: "Reviews typically take 2-10 minutes depending on model and input size"

---

## 5. Version number too small in header under gear icon

**Problem:** Tiny version display.

**Files:** `src/devils_advocate/gui/static/` (CSS) and/or `src/devils_advocate/gui/templates/base.html`

**Fix:** Increase font size or reposition the version string.

---

## 6. Author response block label: model ID inside content body

**Problem:** Reviewer feedback uses a generic label with model ID at the start of the content body. Author response bakes the model ID into the collapsible label. Should be consistent.

**Files:** `src/devils_advocate/gui/templates/review_detail.html` -- the author response section rendering

**Fix:** Move model ID from the label into the response body, matching the reviewer feedback pattern.

---

## 7. [Low] Duplicate log lines on some events

**Problem:** Some log messages appear twice in the live console during a review. The monkey-patched `storage.log` in the runner fires alongside the original logger.

**Files:** `src/devils_advocate/gui/runner.py` -- the log monkey-patch

**Fix:** Ensure the patch replaces rather than supplements the original log call, or deduplicate at the event queue level.

---

## 8. [Low] Total elapsed time on details page

**Problem:** No elapsed time shown on completed review detail pages.

**Files:** `src/devils_advocate/gui/templates/review_detail.html`, `src/devils_advocate/gui/pages.py`

**Fix:** Compute elapsed from first and last log timestamps (or review start/end in ledger). Display in the summary strip.

---

## 9. Remove accept/override buttons in spec mode detail view

**Problem:** Spec mode is non-adversarial (no author, no governance, no escalations) but the detail page shows "Accept Reviewer", "Accept Author", "Keep Open" buttons that have no meaning.

**Files:** `src/devils_advocate/gui/templates/review_detail.html`

**Fix:** Conditionally hide the override buttons when `mode == "spec"`.

---

## 10. Code review: apply diff to produce full revised file + LLM fallback

**Problem:** Code review revision produces a diff which has limited usability. Users want the full revised file.

**Files:**
- `src/devils_advocate/revision.py` -- add post-revision step
- `src/devils_advocate/gui/templates/review_detail.html` -- add "Patch" button
- `src/devils_advocate/gui/api.py` -- endpoint for patch application and LLM fallback

**Fix:**
1. After revision produces `revised-diff.patch`, attempt `subprocess.run(['patch', '--dry-run', ...])` against the original file.
2. If dry-run succeeds, apply for real and save as `revised-<original_filename>`.
3. If dry-run fails, show warning on detail page and offer "Regenerate as full file" button.
4. That button triggers a new LLM call to the revision role with original code + accepted findings, producing the full rewritten file (same approach as plan/spec revision).

---

## 11. Example models.yaml needs sane max_out defaults

**Problem:** Fresh `dvad config --init` produces a models.yaml without `max_out_stated` or `max_out_configured` values. Users inherit unlimited output tokens by default, leading to runaway generation and cost.

**Files:**
- `src/devils_advocate/examples/models.yaml.example`

**Fix:** Add `max_out_stated` and `max_out_configured` with correct values for every model in the example config. Research current stated limits for each provider/model.

---

## 12. [Doc] Self-hosted / enterprise deployment guide

**Problem:** Users with data governance requirements don't realize dvad already supports internal-only endpoints via `api_base` in models.yaml, cost caps via `--max-cost`, and provider lockdown by simply not configuring external providers.

**Files:** New doc: `docs/enterprise-deployment.md`

**Content:**
- Pointing all models at internal OpenAI-compatible endpoints (Ollama, vLLM, llama.cpp)
- Setting `api_base` per model
- Cost budgets with `--max-cost`
- Air-gapped configuration (no external calls)
- Provider allowlisting by config

---

## 13. E2E tests for max_out_configured enforcement

**Problem:** No test coverage for `max_out_configured` being respected across roles and review modes.

**Files:** `tests/e2e/` -- new test file or additions to existing

**Tests:**
- Sane limit (e.g. 10,000) -- confirm output tokens <= limit
- Insanely small limit (e.g. 1,500) -- confirm enforcement and graceful handling when output is truncated
- Verify across roles: reviewer, author, dedup, revision
- Verify extraction failure path when output is too small for delimiters
- Verify `revision_raw.txt` is populated regardless of extraction success
