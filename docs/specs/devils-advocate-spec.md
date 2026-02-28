# Functional Specification: Devil's Advocate (dvad)

> **Source:** 37 files, 10,539 lines (Python + JS)
> **Spec:** 356 lines | **Compression ratio:** 29.6:1
> **Generated:** 2026-02-27

## Table of Contents

1. [Core (CLI, Config, Types, IDs)](#core-cli-config-types-ids)
2. [Provider Dispatch, Cost Estimation & Service Management](#provider-dispatch-cost-estimation--service-management)
3. [Orchestrator](#orchestrator)
4. [Parsing & Normalization](#parsing--normalization)
5. [Output, Storage & Governance](#output-storage--governance)
6. [GUI (Web Interface)](#gui-web-interface)
7. [Miscellaneous Modules](#miscellaneous-modules)

---

## Core (CLI, Config, Types, IDs)

### CLI (`cli.py`)

- **Entry:** Click group `cli()` with subcommands: `review`, `revise`, `history`, `config`, `override`, `gui`, `install`, `uninstall`.
- **Review lifecycle:** CLI args + `load_config()` → asyncio event loop → orchestrator coroutine → persists ledger to `.dvad/reviews/{review_id}/`. Signal handlers (SIGTERM/SIGINT) guarantee lock release via `_cleanup()` before exit.
- **Revise:** Loads existing review ledger + `original_content.txt` → async revision coroutine → writes revised artifact. Requires completed review.
- **History:** Renders `rich.Table` of all reviews or `rich.Markdown` of single review report.
- **Override:** Manually resolves an escalated governance decision by point ID.
- **GUI:** Launches FastAPI via uvicorn. Non-localhost bind requires `--allow-nonlocal` flag.
- **Systemd:** `install` writes user service unit, calls `systemctl --user enable+start`. `uninstall` reverses.
- **Exit codes:** 0 = success, 1 = all errors (ConfigError, ValidationError, APIError, CostLimitError, StorageError, FileNotFoundError), 130 = KeyboardInterrupt.

### Config (`config.py`)

- **Search precedence:** explicit path > `./models.yaml` > `$DVAD_HOME/models.yaml` > `~/.config/devils-advocate/models.yaml`. Missing → `ConfigError`.
- **Loading:** `yaml.safe_load()` → `ModelConfig` dataclasses. `config['models']` = active only (referenced in roles + enabled). `config['all_models']` = all parsed.
- **Role resolution:** roles block references must point to existing, enabled models. Normalization defaults to dedup model; revision defaults to author model.
- **Dotenv:** `.env` loaded from same directory as `models.yaml`. Sets `os.environ` only for keys not already present.
- **Init:** Creates `~/.config/devils-advocate/` (0o700), writes `models.yaml` (0o600) + `.env.example`. Idempotent — existing file triggers warning, no overwrite.

### Types (`types.py`)

- **Enums:** `Severity` {CRITICAL..INFO}, `Category` {ARCHITECTURE..OTHER}, `Resolution` {ACCEPTED, REJECTED, PARTIAL, AUTO_ACCEPTED, AUTO_DISMISSED, ESCALATED, OVERRIDDEN, PENDING}.
- **Exceptions:** `AdvocateError` base → `ConfigError`, `APIError`, `CostLimitError`, `StorageError`.
- **Review data model:** `ReviewPoint` → grouped into `ReviewGroup` → elicits `AuthorResponse` → `RebuttalResponse` (CONCUR/CHALLENGE) → `AuthorFinalResponse` → `GovernanceDecision`. Full session in `ReviewResult`.
- **CostTracker:** Monotonic budget state machine: `below_80` → `warned_80` (at 80% of `max_cost`) → `exceeded` (at 100%). Calls `_log_fn` callback on each `add()`. `breakdown()` returns per-model totals.
- **ModelConfig.api_key:** Property reads `os.environ[api_key_env]` at access time — never cached.
- **ReviewContext:** Holds project/timing/ID state. `id_suffix` generated once at `__post_init__`. `make_group_id(index)` and `make_point_id(group_id, point_index)` produce hierarchical IDs.

### IDs (`ids.py`)

- **Review ID format:** `YYYYMMDDThhmmss_<sha256[:6]>_review` — unique per timestamp + content.
- **Group ID format:** `{project}.group_NNN.ddMMMyyyy.HHMM.{suffix}` — suffix is 4-char random alphanumeric.
- **Point ID format:** `{group_id}.point_NNN` — always prefixed by parent group ID.
- **GUID assignment:** `assign_guids()` mutates `ReviewGroup.guid` in-place with UUID4.
- **GUID resolution:** `resolve_guid()` fuzzy-matches LLM-emitted UUIDs to group IDs. Pipeline: exact lookup → regex UUID extraction → edit-distance match (threshold ≤2 chars) → `None`.

### Critical Constraints

1. **Role cardinality:** Exactly one author, exactly one integration_reviewer, ≥2 reviewers, ≥1 dedup model. Dedup model must differ from author.
2. **Budget monotonicity:** `CostTracker.warned_80` and `.exceeded` flags transition `False→True` only; never reset.
3. **Signal safety:** All interrupt/term paths invoke `_cleanup()` (lock release + storage close) before process exit.
4. **Config isolation:** `.env` vars never overwrite existing `os.environ` entries. `api_key` property reads env at call time.
5. **ID hierarchy:** Point IDs are strict children of group IDs by prefix. Group IDs are unique per (project, index, timestamp, suffix).
6. **GUID fuzzy tolerance:** ≤2 character edit distance. Extraction lowercases for matching; original case preserved in lookup map.
7. **Review mode requirements:** All modes require `--project`. Modes plan/code/spec require `--input`. `revise` requires existing ledger.

---

## Provider Dispatch, Cost Estimation & Service Management

### Provider Dispatch (`providers.py`)

- **Routing:** `call_model()` dispatches to `call_anthropic`, `call_openai_compatible`, `call_openai_responses`, or `call_minimax` based on `model.provider` and `model.use_responses_api`. All return `tuple[str, dict]` with dict shape `{"input_tokens": int, "output_tokens": int}`.
- **Thinking/Reasoning injection** varies by provider+model:
  - Anthropic opus-4-6/sonnet-4-6: `{"type": "adaptive"}`
  - Anthropic others: `{"type": "enabled", "budget_tokens": <mode-keyed>}`, max_tokens inflated by budget; budget defaults 8192 if mode not in `_ANTHROPIC_THINKING_BUDGETS`
  - OpenAI (api.openai.com): `{"reasoning_effort": "medium"|"high"}`
  - Moonshot: `{"thinking": {"type": "enabled"}}`
  - MiniMax: `{"reasoning_split": True}`
- **Token param selection:** OpenAI-compatible uses `max_completion_tokens` when model_id starts with `o3`/`o4` or `model.use_completion_tokens` is True; otherwise `max_tokens`.
- **Empty response guard:** Warning logged when response text is empty but `output_tokens > 0` (reasoning consumed entire budget).

### Retry State Machine (`call_with_retry`)

- **Attempts:** Exactly `max_retries + 1` calls before `APIError`.
- **Immediate abort:** HTTP 529 (overload), HTTP 4xx non-429 — raises `APIError`, no retry.
- **Retryable:** HTTP 429 (honors `Retry-After` header, floor `2^attempt + jitter`), HTTP 5xx, `httpx.TimeoutException`, `httpx.ConnectError` — exponential backoff with jitter.
- **Logging:** `log_fn` callback invoked on retry; timeout hint logged on first timeout.

### Cost Estimation (`cost.py`)

- **Stateless pure functions.** No I/O, no error paths.
- **`estimate_tokens(text)`:** `len(text) // 4`, minimum 1.
- **`estimate_cost(model, input_tokens, output_tokens)`:** Linear per-1k pricing from `model.cost_per_1k_input/output`; returns 0.0 if either pricing field is None.
- **`check_context_window(model, text)`:** Returns `(fits, estimated_tokens, limit)`. Threshold fixed at 80% of `model.context_window`. Returns `(True, *, 0)` when `context_window` is None.

### Systemd Service Management (`service.py`)

- **Scope:** User-level systemd service `dvad-gui.service` at `~/.config/systemd/user/`. All `systemctl` calls use `--user`.
- **Binary detection:** `detect_dvad_binary()` checks `sys.executable` sibling first, falls back to `shutil.which`; raises `FileNotFoundError` if neither resolves.
- **Unit template:** `Restart=on-failure`, `RestartSec=5`, `KillSignal=SIGINT`, `TimeoutStopSec=10`, `After=default.target`. Default port 8411.
- **Query wrappers:** `systemctl_is_active` and `systemctl_is_enabled` swallow all exceptions, return False on failure. All other `systemctl_*` wrappers propagate `RuntimeError` with stderr on non-zero exit.

### Critical Constraints

1. **Uniform return shape:** Every provider handler must return `(str, {"input_tokens": int, "output_tokens": int})` — no exceptions.
2. **529 is terminal:** HTTP 529 bypasses retry logic entirely; treated as unrecoverable overload.
3. **Token floor:** `estimate_tokens` never returns 0; minimum is 1.
4. **Context window None-safety:** Missing `context_window` on model always yields `fits=True`.
5. **Platform gate:** `check_platform()` returns error string on non-Linux; None on Linux.
6. **Service path invariant:** Service file location is fixed — not configurable, derived from `SERVICE_NAME` constant.

---

## Orchestrator

### Public API

- **Exports:** `run_plan_review`, `run_code_review`, `run_integration_review`, `run_spec_review` — each returns `ReviewResult | None`.

### Shared Pipeline State Machine (`_common._run_adversarial_pipeline`)

- **States:** reviewer_call → normalization_fallback? → dedup → author_response → round2_rebuttal? → round2_author_final? → governance → revision? → complete | aborted
- **Skip logic:** Round 2 rebuttals skipped if all groups ACCEPTED. Author final skipped if no CHALLENGE verdicts. Revision skipped if no actionable findings.
- **Rebuttal routing:** Only reviewers who sourced contested groups receive rebuttal prompts.
- **Catastrophic fallback:** If <25% of author responses parse successfully, all groups escalated to governance.
- **Normalization fallback:** If `parse_review_response` yields zero points, `normalize_review_response` called with a non-author model.

### Mode-Specific Orchestrators

- **Plan/Code (parallel multi-reviewer):** Pre-flight context window check filters `active_reviewers`. Round 1 via `asyncio.gather`. Dedup skipped if any reviewer failed with multi-reviewer config; points promoted directly to groups instead. Revision output: `revised-plan.md` / `revised-diff.patch`.
- **Integration (single reviewer):** File discovery: explicit `input_files` OR `manifest.json` completed tasks. Spec discovery: explicit OR `000-strategic-summary.md` OR `strategic-summary.md`. Each point promoted to own group (no dedup). Combined content delimited by `--- {path} ---`. Revision output: `remediation-plan.md`. Chunking deferred to v2.
- **Spec (collaborative, non-adversarial):** No author, no rebuttals, no governance. Uses `get_spec_reviewer_system_prompt`, `parse_spec_response`, `run_spec_revision`. Dedup groups by theme with consensus indicators. Summary tracks `multi_consensus`/`single_source` counts. Revision always attempted. `ReviewResult.author_model` set to empty string; governance/author/rebuttal lists empty. Output: `revised-spec-suggestions.md`.
- **Plan/Code input structure:** `input_files[0]` = PRIMARY ARTIFACT; `input_files[1:]` = REFERENCE CONTEXT (flagged as not-for-direct-review).

### Cost Guardrail Protocol

- **Pre-flight:** Estimated cost vs `max_cost` checked before lock acquisition. Exceeds → stub ledger with `cost_exceeded`, return None.
- **Runtime checkpoints:** After Round 1 reviewers, after dedup, after Round 1 author response (in shared pipeline).
- **Warning threshold:** Console warning at 80% of budget (printed once). Abort at 100%.
- **`_estimate_total_cost` covers:** R1 reviewers + dedup + R1 author + R2 rebuttals + R2 author final (half-token estimate) + revision.

### Lock & Storage Discipline

- **Lock acquired** after dry-run and cost pre-flight, before any LLM calls.
- **Lock + storage always released** in `finally` block. Lock failure → immediate `None`.
- **`review_id` generated** from content hash; `storage.set_review_id()` called immediately.
- **Intermediate artifacts:** `round1/`, `round2/` subdirectories. `original_content.txt` persisted for `dvad revise`.
- **Final artifacts:** `dvad-report.md`, `review-ledger.json`, mode-specific revision file.

### Dry Run Protocol

- **Behavior:** Prints cost estimate table (Rich), saves stub ledger with `dry_run` result + `cost_estimate_rows`, returns None. No LLM calls.
- **`_build_dry_run_estimate_rows`** includes normalization step as conditional cost line.

### Display (`_display.py`)

- **`_print_governance_summary` color map:** `auto_accepted`/`accepted` = green, `escalated` = yellow, `auto_dismissed` = cyan, all other = red.
- **Dry run table:** Total cost in bold, color-coded against limit.

### Formatting (`_formatting.py`)

- **Pure functions**, no side effects.
- **`_get_contested_groups_for_reviewer`:** Returns groups where reviewer is in `source_reviewers` AND resolution is not ACCEPTED.
- **`_format_challenged_groups`:** Only includes groups with at least one CHALLENGE verdict.
- **`_group_to_dict`:** `guid` field conditional (included only if present).
- **`_compute_summary`:** Aggregates governance resolution counts + `total_groups` + `total_points`.

### Critical Constraints

1. **Reviewer role labels:** `f"reviewer_{i+1}"` (1-indexed) across all modes.
2. **At least one reviewer** must pass context window check or orchestrator aborts.
3. **All reviewer failures non-fatal** if at least one succeeds; revision failures non-fatal (downgrade to "completed").
4. **Author final response failure non-fatal** — proceeds with Round 1 positions.
5. **Stub ledgers saved** for dry_run, cost_exceeded, cost_aborted, and failed reviews via `_save_stub_ledger`.
6. **Report and ledger saved before revision** in shared pipeline.
7. **Spec mode cost estimate** excludes Round 2 (no adversarial exchange).

---

## Parsing & Normalization

### Response Parsing (`parser.py`)

- **Pure transforms:** All functions are stateless regex-based extractors. No persistent state, no I/O beyond optional `log_fn` callback.
- **Normalization maps:** Severity → {critical, high, medium, low, info} (default: medium). Category → {architecture, security, performance, correctness, maintainability, error_handling, testing, documentation, other} (default: other). Theme → {ux, features, integrations, data_model, monetization, accessibility, performance_ux, content, social, platform, security_privacy, onboarding, other} (default: other). Hyphen/space/underscore collapsed before lookup.
- **Resolution enums:** Author R1 → ACCEPTED|REJECTED|PARTIAL|UNKNOWN. Rebuttal → CHALLENGE|CONCUR. Author final → MAINTAINED. Unparseable defaults to UNKNOWN/CONCUR/MAINTAINED respectively.
- **Dedup grouping:** `_parse_grouped_response` drives both plan and spec dedup via callback injection (`extract_fields`, `build_group_attrs`, `build_singleton_attrs`). First-claim-wins: once a point maps to a group, no reassignment. Unclaimed points auto-promote to singleton groups.
- **ID resolution:** Group references use `resolve_guid()` from `.ids`. Positional fallback: "GROUP N [uuid]" tries UUID match first, falls back to index N.
- **Reasoning strip:** `<thinking>`, `<reasoning>`, `**Thinking:**` blocks removed before any parsing.
- **Multiline extraction:** `_extract_multiline_field` uses regex lookahead against next field delimiters to capture spanning values.
- **Skip conditions:** Empty description → skip block silently. Missing delimiters in `extract_revised_output` → return empty string.

### LLM Normalization Fallback (`normalization.py`)

- **Trigger:** `normalize_review_response` fires only when regex parsing yields zero ReviewPoints.
- **Pipeline:** Builds normalization prompt → `call_with_retry()` to LLM → `parse_review_response()` on result.
- **Fault behavior:** Catches all exceptions, logs, returns empty list. Never raises.

### Deduplication (`dedup.py`)

- **Mode dispatch:** `mode="spec"` → `format_suggestions_for_dedup` + `build_spec_dedup_prompt` + `parse_spec_dedup_response`. All other modes → `format_points_for_dedup` + `build_dedup_prompt` + `parse_dedup_response`.
- **Context overflow guard:** `check_context_window` before LLM call. Overflow → `promote_points_to_groups` (1:1 point→group wrapping), no LLM call.
- **Serialization difference:** Point format includes severity/recommendation; suggestion format omits both.
- **Empty input → empty output.** LLM failures propagate (caller handles).

### Prompt Assembly (`prompts.py`)

- **Template engine:** `load_template(name, **kwargs)` reads from `devils_advocate.templates/` via `importlib.resources`, applies `str.format()`. Missing file or variable → `AdvocateError` (no silent fallback).
- **Cache:** `get_reviewer_system_prompt()` and `get_spec_reviewer_system_prompt()` lazy-load once into module globals.
- **Mode-branched templates:** Round 1 author: plan → `round1-author-plan-instruct.txt`, else → `round1-author-code-instruct.txt`. Final author: plan → `round2-author-final-plan-instruct.txt`, else → `round2-author-final-code-instruct.txt`.
- **Governance blocks:** Injected via `_load_governance_block()` / `_load_governance_final_block()`.

### Revision Engine (`revision.py`)

- **Governance filtering:** `build_revision_context` groups points by group_id, classifies by final_resolution: {auto_accepted, accepted, overridden} → actionable; auto_dismissed → dismissed; else → unresolved. Dedupes on (description, recommendation, location, reviewer) tuples.
- **Early exits:** `run_revision` skips if no "=== ACCEPTED FINDINGS" in context. `run_spec_revision` skips if context is blank. Context overflow → empty string. Missing output delimiters → empty string. All non-fatal.
- **Delimiter map:** plan → `=== REVISED PLAN ===`, code → `=== UNIFIED DIFF ===`, integration → `=== REMEDIATION PLAN ===`, spec → `=== SPEC SUGGESTIONS ===`.
- **Token budget:** Uses `REVISION_MAX_OUTPUT_TOKENS` (64000). Warns if estimated time >120s (~10 tokens/sec heuristic).
- **Spec mode:** Includes all groups unconditionally (no governance filter). Organized by theme.

### Critical Constraints

1. **Normalization totality:** All three normalization maps (severity, category, theme) are total functions — every input maps to a valid canonical value via defaults.
2. **First-claim-wins:** Point assignment in grouped parsing is non-revocable; duplicated point references in later groups are silently ignored.
3. **Error asymmetry:** Parser and normalization swallow errors (skip/empty-list). Dedup and revision LLM call failures propagate. Revision extraction failures return empty string.
4. **Prompt integrity:** Template substitution failures are hard errors (`AdvocateError`), never degraded prompts.
5. **Context window gating:** Both dedup and revision check context window before LLM calls; overflow triggers graceful degradation (promote-to-singletons or skip-revision), never truncation.

---

## Output, Storage & Governance

### Report Generation (`output.py`)

- **Pure transforms:** `generate_report()` → markdown string; `generate_ledger()` → JSON-serializable dict. Idempotent, no I/O.
- **Report structure:** Fixed section order: header metadata (mode, inputs, project, timestamp, review_id, models, cost) → summary table → escalated items → review points → revised output → cost breakdown.
- **Round display rules:** Author Round 1 response always shown (fallback message if missing). Rebuttals shown only if present. Author final response shown only if group has challenges.
- **Ledger shape:** Flat point list; each point carries full governance metadata (author_resolution, rebuttals, final_resolution, governance_resolution).
- **Spec mode:** Groups suggestions by theme, sorts by consensus count descending, surfaces "High-Consensus Ideas" for multi-reviewer agreement.
- **Lookup construction:** `_build_lookup_maps()` indexes decisions/responses/rebuttals/final_responses by group_id; missing keys → empty/placeholder defaults.

### Storage (`storage.py` — `StorageManager`)

- **Data dir resolution:** `$DVAD_HOME` → `~/.local/share/devils-advocate/` (XDG fallback). Subdirs: `reviews/`, `logs/`, `lock_dir/`.
- **Locking:** Atomic via `O_CREAT | O_EXCL` on `.dvad/.lock`. Lock file contains `{pid, hostname, timestamp}` JSON. Max 3 acquisition attempts. Stale if age > 3600s or PID dead on same host (verified via `kill(pid, 0)` + hostname match). Corrupted lock JSON → treated as stale.
- **Atomic writes:** All file writes use `mkstemp` in target directory → `write` + `fsync` → `os.replace`. Temp file cleaned up on failure.
- **Logging:** Lazy file open on first `log()` call. Each line UTC-timestamped, flushed immediately. Falls back to `session.log` if `set_review_id()` never called.
- **Review directory:** `reviews/{review_id}/` always created with `round1/`, `round2/`, `revision/` subdirs.
- **Artifacts:** `save_review_artifacts()` writes `dvad-report.md` + `review-ledger.json`. `save_intermediate()` writes raw text or JSON to stage subdirs.
- **Point override:** `update_point_override()` appends to `overrides` array and updates `final_resolution` in ledger. Raises `StorageError` if review/point/group not found.
- **Listing:** `list_reviews()` scans review dirs, parses ledgers; silently skips JSON decode failures.
- **Manifest:** `load_manifest()` reads `.dvad/manifest.json`; no corresponding write method exists.

### Governance (`governance.py`)

- **Core invariant:** No finding passes without demonstrated author engagement. Implicit acceptance → ESCALATED. Rote acceptance (regex-matched phrases or <15 words) → ESCALATED.
- **Resolution override:** Final response (Round 2) overrides Round 1 for challenged groups only.
- **Multi-reviewer consensus (≥2):** Rejection or MAINTAINED must pass 3-criteria validation (technical reason + mechanism + reference) or point auto-accepted.
- **Single-reviewer:** Rejection → auto-dismissed. Exception: challenged rejection → ESCALATED; integration mode rejection → ESCALATED.
- **Acceptance validation:** `validate_acceptance()` requires ≥`ACCEPTANCE_MIN_WORDS` (15) and no match against `ROTE_ACCEPTANCE_PHRASES` regexes.
- **Rejection validation:** `validate_rejection()` checks 3 criteria; defaults to invalid (safe) on ambiguity.
- **MAINTAINED:** Treated identically to rejection for validation; multi-reviewer requires 3-criteria or auto-accept; single-reviewer → ESCALATED.
- **Partial acceptance:** Always ESCALATED (not incorporated into revision).
- **Unknown resolution:** Lowercased, ESCALATED, reason "Unrecognized resolution".
- **Mode parameter:** Only `integration` has distinct behavior (stricter single-reviewer rejections). `spec` and `plan` share default logic.

### UI (`ui.py`)

- **Single export:** `console` — Rich `Console` singleton instantiated at import time. No state, no side effects until methods called by consumers.

### Critical Constraints

1. **Crash safety:** Every filesystem write is atomic (`mkstemp` + `fsync` + `os.replace`, same-filesystem guarantee). No partial writes survive.
2. **Lock race-freedom:** `O_CREAT | O_EXCL` guarantees exactly one winner. Stale detection uses both PID liveness and host identity — cross-host staleness detected only by age.
3. **Governance determinism:** Pure function — same inputs always produce same `GovernanceDecision` list. No randomness, no I/O, no mutable state.
4. **Escalation bias:** System defaults to ESCALATED for any ambiguous or missing author engagement (no response, rote response, unknown resolution, challenged-without-final).
5. **Log durability:** Lines flushed immediately after write; survives process crash between log calls.
6. **Report idempotency:** `generate_report()` and `generate_ledger()` are pure transforms; repeated calls on same `ReviewResult` produce identical output.

---

## GUI (Web Interface)

### App Bootstrap (`__init__.py`, `app.py`)

- **Factory:** `create_app(config_path)` / `create_app_from_env()` (reads `DVAD_E2E_CONFIG`) → FastAPI with static mount, Jinja2 templates, ReviewRunner on `app.state`
- **Lifespan:** startup seeds `app.state.csrf_token` (32-byte `token_urlsafe`, immutable), `app.state.runner`, `app.state.templates` (with `human_date` filter). Shutdown cancels `current_task` if running, suppresses all cleanup exceptions.

### API Endpoints (`api.py`)

#### Review Lifecycle
- **`POST /api/review/start`:** Multipart form → validates mode (plan|code|integration|spec), collects files (path-based via JSON arrays or upload-based via multipart), returns `{review_id}`. 409 if review already running. File constraints: plan/spec require ≥1 file, code requires exactly 1. Limits: 10 MB/file, 25 files max.
- **`GET /api/review/{id}/progress`:** SSE stream. Replays buffered events first, then live queue. 15s keepalive ping. Terminal on `complete`/`error` event type. Late-connecting clients receive full buffer.
- **`POST /api/review/{id}/cancel`:** Cancels running review task.
- **`POST /api/review/{id}/override`:** Override escalated group resolution.
- **`POST /api/review/{id}/revise`:** Loads ledger + `original_content.txt`, calls `run_revision()`, saves artifact. Skipped if no original content or mode not in (plan, code, integration). Returns `{status, content, filename, cost}`.
- **Downloads:** `/report` → `dvad-report.md`, `/revised` → `revised-plan.md`/`revised-diff.patch`, `/log` → plaintext log.

#### Config Management
- **YAML mutations:** Load with ruamel.yaml (preserves comments/quotes) → mutator modifies in-place → atomic write via `StorageManager._atomic_write`. Endpoints: model-timeout, model-thinking, model-max-tokens, settings-toggle.
- **`POST /api/config`:** Full YAML save. **`POST /api/config/validate`:** Returns `{valid, issues}`.
- **`.env` operations:** CRUD on env vars. Atomic write with `0o600` permissions. Updates `os.environ` in-process. Key names whitelisted against `api_key_env` in models config.
- **`GET /api/fs/ls`:** Directory listing for file picker.

### Pages (`pages.py`)

- **Dashboard (`/`):** Paginated (25/page) review list, 5s TTL cache (`_list_reviews_cached`). `show_test` filter excludes "test" projects. Sorted newest-first. Shows config health warnings.
- **Review Detail (`/review/{id}`):** Running → progress UI (no ledger). Complete/failed → loads ledger, groups points by `group_id` (fallback `point_id`), classifies by `final_resolution` (escalated, auto_accepted, auto_dismissed, overridden). Unknown resolution defaults to escalated. Cost table: dry-run/failed → all zeros; success → per-role costs from `ledger.cost.role_costs`. Reviewers labeled "reviewer 1"/"reviewer 2" when >1. Review not found → redirect to `/`.
- **Config (`/config`):** Loads config + raw YAML, groups models by vendor (inferred from `api_base` hostname > `provider` string), resolves storage paths. Renders `model_vendors`/`model_thinking` maps for JS.

### Progress Events (`progress.py`)

- **Pure functions.** `classify_log_message(msg)` matches against ordered `_PHASE_PATTERNS` regex list → `ProgressEvent(event_type, message, phase, detail, timestamp)`.
- **Cost events:** type=`cost`, message="" (suppressed from console). Format: `§cost role=<role> model=<model> cost=<cost> total=<total>`.
- **Phases detected:** round1 (calling/responded/normalization) → dedup → round2 (skip/rebuttal failures) → governance → revision. Cost warnings at 80% threshold.
- **Fallback:** Always returns valid `ProgressEvent` with type=`log`, phase=`unknown`.

### ReviewRunner (`runner.py`)

- **Concurrency:** Single-review-at-a-time. `current_task` gate → 409 on second start.
- **Review ID:** Content hash of input files (deterministic).
- **Event pipeline:** `StorageManager` log hook monkey-patched → `emit_event()` → appends to buffered list + pushes to `asyncio.Queue(maxsize=500)`. Queue full → oldest dropped.
- **Background task (`_run`):** Loads config → creates StorageManager → saves manifest + input copies → emits metadata (role→model map) → dispatches to `run_{plan,code,integration,spec}_review` → marks complete/failed → emits terminal event.
- **Cancellation:** `task.cancel()` → `CancelledError` caught → stub ledger saved (best-effort).
- **Failure:** Any exception → stub ledger saved, terminal error event emitted.

### Client App (`static/app.js`)

#### Review Submission
- **Flow:** form_editing → interstitial (CLI command preview) → submitting → redirect to `/review/{id}`.
- **Mode switching:** plan → shows reference files; code → single file; spec → multi-select; integration → optional files + project_dir.

#### SSE Client
- **Flow:** EventSource opens → processes events (metadata → build cost table, cost → update cells, phase → update dots, log → append) → terminal → close + reload (500ms success, 3s error). Ignores keepalive parse errors.

#### File Picker
- **Modal state machine:** closed → open → selecting → confirmed → closed. Single-click selects, double-click navigates directories. `_selectedPaths` persisted across modal open/close cycles. Supports multi-select and dir-mode.

#### Role Assignment & Thinking Icons
- **Role pills:** Radio-select for singular roles (author, dedup, normalization, revision, integration_reviewer); checkbox with max=2 for reviewer. Click toggles `role-active` class. Dirty state triggers save toast.
- **`saveRoleAssignments`:** Scans DOM for active pills → builds roles object → parses YAML with js-yaml → updates `config.roles` → serializes → POST to `/api/config`.
- **Thinking icons:** Click active only when model has role assignment (`thinking-eligible`). Toggling hits `/api/config/model-thinking`.
- **Idempotent init:** `_rolePillsInitialized` / `_thinkingToggleInitialized` flags guard double-attachment.

#### Config UI
- **Tabs:** `switchTab(tab)` switches config page sections.
- **Inline editors:** Timeout (10–7200), max_tokens (1–1000000). Blur/keydown handlers ensure single active editor.
- **Env keys:** Password-type inputs, double-click toggles visibility. Whitelist-only: variables must be declared in models config `api_key_env`.
- **YAML editor:** `validateYaml()` / `saveYaml()` round-trip through API.

#### Misc UI
- **Phase dots:** reviewers → dedup → author → rebuttals → governance → revision. Spec mode hides adversarial phases (rebuttals, governance).
- **Override pipeline:** All escalated groups must be resolved before revision button activates.
- **Table sorting:** Column header click toggles asc/desc with ▲/▼ indicators.
- **Vendor icons:** Anthropic=gem, OpenAI=sparkles, Google=globe, xAI=zap, DeepSeek=compass, Moonshot=moon, MiniMax=box.

### Critical Constraints

1. **CSRF:** All mutating endpoints validated via `_check_csrf`. Token from `<meta name="csrf-token">`, generated once at startup.
2. **Single review:** Runner enforces one concurrent review via `current_task` check (409 on conflict).
3. **Atomic writes:** Config YAML and `.env` mutations use atomic write — never partial state on disk.
4. **Event durability:** Buffered events persist for late-connecting SSE clients; queue overflow drops oldest.
5. **File limits:** MAX_FILE_SIZE=10MB, MAX_FILES=25. Path-based input uses files in-place; upload mode copies to tmpdir.
6. **Env security:** `.env` written with `0o600`. Key names whitelisted against model config `api_key_env`.
7. **Idempotent UI init:** Role pills and thinking toggle guard double-attachment via initialized flags.
8. **Dashboard cache:** 5s TTL on review list (`_CACHE_TTL = 5`).

---

## Miscellaneous Modules

### Package Markers (templates/__init__.py, examples/__init__.py)

- **Behavior:** Empty `__init__.py` files. No exports, no executable code.

### OpenAI Model Probe (scripts/probe_openai_models.py)

- **Purpose:** Standalone diagnostic script that smoke-tests OpenAI API connectivity for all configured models.
- **Discovery:** Loads `models.yaml` via `load_config()`, filters to `provider=="openai"` with `api.openai.com` in `api_base`.
- **Probing:** For each model, POSTs `PROBE_PROMPT` ("Reply with exactly: PROBE OK") to `/chat/completions`. Models with "codex" or "pro" in `model_id` also probe `/responses` endpoint.
- **Token param selection:** Uses `max_completion_tokens` when `model_cfg.use_completion_tokens` is truthy, else `max_tokens`.
- **Result shape:** `{api, status, http, error, snippet, tokens}` — status is PASS (<400) or FAIL (>=400 or exception); error truncated to 120 chars, snippet to 80 chars.
- **Output:** Formatted stdout table with status icons per model per API.
- **Execution model:** `asyncio.run(main())` with single shared `httpx.AsyncClient`; probes run serially, not fanned out.

### Critical Constraints

1. **sys.path mutation:** Script prepends repo root to `sys.path` before imports to support non-installed execution.
2. **Missing API key:** Skipped silently with custom message — no exception raised.
3. **Timeout:** All HTTP requests default to 30s; `TimeoutException` caught and returned as FAIL.
4. **Graceful degradation:** Missing response keys (`choices`, `output`, `content`) default to empty strings / 0 tokens — no crash.
