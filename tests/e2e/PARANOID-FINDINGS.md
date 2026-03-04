# Paranoid E2E Test Findings

Generated: 2026-03-03
Source: `tests/e2e/test_paranoid_*.py` (126 tests, 107 passed, 11 xfailed)

---

## Summary

The paranoid E2E layer audits every state-mutating endpoint in the dvad GUI for
unguarded destructive behavior. These are the findings - endpoints that can
destroy data without confirmation, backup, or undo.

---

## Critical Findings

### 1. Batch env save silently deletes API keys

- **Endpoint**: `POST /api/config/env`
- **File**: `src/devils_advocate/gui/api.py`, line ~1096
- **Problem**: When the batch save receives an empty string value for a key, it
  removes that key from both the `.env` file and `os.environ`. If a frontend
  form sends untouched fields as `""`, existing API keys get wiped.
- **Impact**: Loss of API keys with no warning, no confirmation, no recovery.
- **Fix**: Reject empty string values with a 400 error, or skip keys with empty
  values instead of treating them as delete instructions.

### 2. Full config overwrite with no backup

- **Endpoint**: `POST /api/config`
- **File**: `src/devils_advocate/gui/api.py`, line ~806
- **Problem**: Overwrites the entire `models.yaml` with the submitted YAML
  content. No backup copy is made. No confirmation dialog. No undo.
- **Impact**: A malformed save or accidental submission wipes the full model
  configuration. The user must manually recreate it.
- **Fix**: Write a timestamped backup (e.g. `models.yaml.bak`) before
  overwriting, or keep the last N versions in a backup directory.

### 3. Single env key delete with no confirmation

- **Endpoint**: `DELETE /api/config/env/{env_name}`
- **File**: `src/devils_advocate/gui/api.py`, line ~1037
- **Problem**: Permanently removes an API key from `.env` and `os.environ`. No
  confirmation prompt. The key value is gone unless the user remembers it.
- **Impact**: Accidental click deletes a key with no recovery path.
- **Fix**: Add a confirmation step in the frontend, or implement soft-delete
  (comment out the line in `.env` instead of removing it).

---

## Moderate Findings

### 4. Review cancel with no confirmation

- **Endpoint**: `POST /api/review/{id}/cancel`
- **File**: `src/devils_advocate/gui/api.py`, line ~244
- **Problem**: Cancels a running review immediately. Partial artifacts may
  remain on disk. The review cannot be resumed.
- **Impact**: Accidental cancel loses an in-progress review that may have been
  running for minutes and consuming API credits.
- **Fix**: Add a confirmation dialog in the frontend before sending the cancel
  request.

### 5. max_out_configured null removal

- **Endpoint**: `POST /api/config/model-max-tokens`
- **File**: `src/devils_advocate/gui/api.py`, line ~708
- **Problem**: Sending `max_out_configured: null` removes the key entirely from
  `models.yaml` rather than setting it to a default. This is by design but
  undocumented - a frontend bug that sends null instead of a value silently
  removes the constraint.
- **Impact**: Model output limit disappears without feedback.
- **Fix**: Document the behavior, or require an explicit "remove" action rather
  than accepting null as a delete signal.

---

## Low Risk (documented for completeness)

### 6. Review start creates state without confirmation

- **Endpoint**: `POST /api/review/start`
- **Problem**: Creates a new review directory and begins processing. No
  confirmation dialog.
- **Impact**: Minimal - creates new state, doesn't destroy existing data. The
  user explicitly clicked "Start Review."
- **Action**: No fix needed. Documented for registry completeness.

### 7. Override can re-escalate dismissed findings

- **Endpoint**: `POST /api/review/{id}/override`
- **Problem**: Setting resolution to `"escalated"` on a previously dismissed
  finding re-opens it. This is valid behavior but could be surprising.
- **Impact**: Minimal - the override history is preserved in the ledger.
- **Action**: Consider whether the frontend should prevent re-escalation, or
  document it as intentional.

---

## How to use this file

Feed this document into a new session with the instruction:

> Fix the paranoid E2E findings documented in
> `tests/e2e/PARANOID-FINDINGS.md`. Start with the critical findings.
> After each fix, convert the corresponding xfail test in
> `tests/e2e/test_paranoid_loss_annotations.py` to a real assertion,
> then run the paranoid suite to verify:
> `.venv/bin/python -m pytest tests/e2e/test_paranoid_*.py -m e2e -v`

---

## Test file reference

| File                                | Tests | Purpose                                                                  |
| ----------------------------------- | ----- | ------------------------------------------------------------------------ |
| `paranoid_helpers.py`               | -     | Write registry (14 endpoints), loss annotations, snapshot infrastructure |
| `test_paranoid_inventory.py`        | 38    | Registry completeness, CSRF enforcement, empty payload rejection         |
| `test_paranoid_snapshots.py`        | 11    | Round-trip idempotency, ledger integrity, env file isolation             |
| `test_paranoid_fuzzing.py`          | 51    | Boundary values, type confusion, degenerate inputs                       |
| `test_paranoid_loss_annotations.py` | 26    | Policy enforcement, preconditions, findings documentation                |
