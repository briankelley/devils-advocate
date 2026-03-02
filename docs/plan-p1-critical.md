# P1 Implementation Plan: Critical Fixes

## 1. max_out_configured not enforced on revision calls

### Problem
The revision role ignores `max_out_configured` from models.yaml. The hardcoded output limit in `providers.py` (64,000 tokens for revision) always wins. This causes runaway generation, $0.99+ wasted per review, 12+ minute waits, and extraction failures when the response truncates without closing delimiters.

### Evidence
- Two consecutive code reviews: both produced 64,000 output tokens despite `max_out_configured: 10000` in the model config.
- Extraction failed both times (canonical delimiters not found).
- `revision_raw.txt` was zero bytes (raw response not saved before extraction).

### Files to modify
- `src/devils_advocate/providers.py` -- where output limits are set per role. Find where the revision role hardcodes 64,000 and make it respect `min(hardcoded_limit, model.max_out_configured)` when `max_out_configured` is set.
- `src/devils_advocate/revision.py` -- save raw response text BEFORE attempting delimiter extraction. If extraction fails, the raw response should still be on disk for inspection.

### Implementation steps
1. Read `providers.py`, locate the output limit logic (look for 64000/32000/16384 constants and how role maps to limit).
2. Modify the limit selection: if `model.max_out_configured` is set and is less than the hardcoded role limit, use the model's configured value.
3. In `revision.py`, write the raw LLM response to `revision_raw.txt` immediately upon receipt, before any delimiter parsing.
4. Add unit tests: mock a model with `max_out_configured=5000` and verify the API call receives `max_tokens=5000` for a revision role call.
5. Add E2E tests (see plan-p2-minor.md #13 for full E2E scope on this).

### Validation
- Run a code review with `max_out_configured: 10000` on the revision model.
- Confirm log shows output tokens <= 10,000.
- Confirm `revision_raw.txt` is populated even if extraction fails.
- Confirm revision completes in under 2 minutes for a small file.

---

## 2. Post-override revision regeneration

### Problem
After a user overrides escalated findings on the detail page (accepting reviewer or author), the "Download Revised" button still serves the original revision artifact. There is no way to regenerate the revision incorporating the override decisions. The plumbing exists (`POST /api/revise`, the overrides banner, the CLI `revise` command) but the UI flow is broken.

### Evidence
- Accepted 3 of 5 escalations for author, accepted reviewer on #4.
- Exited detail view, returned from history -- still shows "Download Revised" with the stale artifact.
- No prompt to regenerate.

### Files to modify
- `src/devils_advocate/gui/templates/review_detail.html` -- button state logic. When overrides exist that post-date the revision timestamp, show "Regenerate Revised" instead of "Download Revised".
- `src/devils_advocate/gui/api.py` -- the `POST /api/revise` endpoint already exists. May need to accept a flag indicating this is a re-revision after overrides.
- `src/devils_advocate/gui/pages.py` -- pass override timestamps vs revision timestamp to the template context.
- `src/devils_advocate/storage.py` -- may need to expose override timestamps from the ledger.

### Implementation steps
1. Read the ledger JSON structure to understand how overrides and revision timestamps are stored.
2. In `pages.py` review_detail handler, compare latest override timestamp against revision file mtime (or a stored revision timestamp in the ledger).
3. If any override is newer than the revision: set `revision_stale=True` in template context.
4. In `review_detail.html`: when `revision_stale`, change button text to "Regenerate Revised" and wire it to call `POST /api/revise`.
5. When no revision exists at all (extraction failed), keep existing "Generate Revision" button behavior.
6. After successful re-revision, refresh the page to show updated "Download Revised".

### Validation
- Run a plan review, let it complete with escalations.
- Override one escalation (accept reviewer).
- Confirm button changes to "Regenerate Revised".
- Click it, confirm new revision incorporates the override.
- Confirm "Download Revised" returns the updated artifact.
