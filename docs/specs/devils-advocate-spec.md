# Functional Specification: Devil's Advocate (dvad)

> **Source:** 41 files, 12,643 lines (Python + JS + HTML + CSS)
> **Spec:** 266 lines | **Compression ratio:** 47.5:1
> **Generated:** 2026-02-28

---

## Table of Contents

1. [Core Pipeline](#core-pipeline)
2. [Orchestrator](#orchestrator)
3. [GUI Backend](#gui-backend)
4. [GUI Frontend](#gui-frontend)

---

## Core Pipeline

### CLI Entry (`cli.py`)

- **Commands:** `review`, `revise`, `history`, `config`, `override`, `gui`, `install`, `uninstall`
- **Lifecycle:** config load → validate → acquire lock → `asyncio.new_event_loop()` → orchestrator coroutine → cleanup
- **Signal handling:** `loop.add_signal_handler()` (POSIX) with `signal.signal()` fallback; `_cleanup()` guarantees lock release + storage close on any exit path
- **Exit codes:** 0 success, 1 error (ConfigError/APIError/CostLimitError/file-not-found), 130 SIGINT
- **Revise precondition:** Round 1 response required — sourced from ledger or `--input` override

### Configuration (`config.py`)

- **Search priority:** explicit path → discovery chain → `~/.config/devils-advocate/models.yaml`
- **Dotenv:** `.env` loaded from config dir into `os.environ` before key resolution
- **Role defaults:** normalization → dedup model; revision → author model
- **Validation rules:** exactly 1 author, ≥2 reviewers, 1 integration_reviewer, ≥1 dedup; dedup ≠ author; all role refs must exist in `models` block and be enabled; missing API key env var → validation error

### Cost & Context (`cost.py`)

- **Token estimate:** `len(text) / 4`, minimum 1
- **Context window:** 80% threshold (`CONTEXT_WINDOW_THRESHOLD = 0.8`)
- **Cost:** returns 0.0 when model lacks cost config

### Deduplication (`dedup.py`)

- **Constraint:** each point belongs to at most one group
- **Fallback:** LLM failure or context overflow → every point promoted to singleton group
- **IDs:** group and point IDs assigned via `ReviewContext`

### Governance (`governance.py`)

- **Core invariant:** no finding passes without demonstrated author engagement
- **Escalation triggers:** no response, rote acceptance (<15 words or stock phrase), invalid rejection against ≥2 reviewers, integration-mode single-reviewer rejection
- **Rejection validity:** requires all three of: technical reason, mechanism, reference
- **Round precedence:** final response supersedes Round 1 for challenged groups
- **AUTO_DISMISSED:** only for single-reviewer rejection unchallenged, never in integration mode

### ID Generation (`ids.py`)

- **Formats:** review `YYYYMMDDThhmmss_{sha256-6}_review`; group `project.group_NNN.ddMMMyyyy.HHMM.suffix`; point `group_id.point_NNN`
- **GUID resolution:** UUID4 assigned per group for prompt correlation; fuzzy match tolerates ≤2 character differences

### Parsing (`parser.py`)

- **Strictly synchronous** — zero async, zero provider calls; all LLM fallback lives in `normalization.py`
- **First-claim-wins:** no point appears in multiple groups; ungrouped → singleton
- **ID resolution chain:** exact → UUID extraction → fuzzy (≤2 diff) → positional → failure

### Normalization (`normalization.py`)

- **Trigger:** synchronous parser yields 0 points
- **Failure mode:** returns empty list (non-fatal)

### Providers (`providers.py`)

- **Dispatchers:** `call_model()` routes to `call_anthropic`, `call_openai_compatible`, `call_openai_responses`, `call_minimax`
- **Token limits:** default 16384; author response 32000; revision 64000
- **Thinking budgets:** spec 4096, plan/code/integration 10000, revision 16000
- **Retry policy:** 529 → immediate `APIError` (no retry); 429 → `Retry-After` or exponential backoff; 5xx/timeout → exponential backoff with jitter

### Revision (`revision.py`)

- **Actionable resolutions filter:** `{auto_accepted, accepted, overridden}` only
- **Skip conditions:** no actionable findings or context overflow
- **Delimiter policy:** canonical delimiters only — no fallback to `PART 2` or markdown headings

### Storage (`storage.py`)

- **Data dir:** `$DVAD_HOME` fallback `~/.local/share/devils-advocate/`; lock dir `{project_dir}/.dvad/`
- **Lock acquisition:** atomic via `O_CREAT|O_EXCL`; stale if age >3600s or dead PID on same host
- **Write safety:** temp file → `fsync` → `os.replace`
- **Artifact layout:** `{review_id}/round1/`, `round2/`, `revision/`

### Output (`output.py`)

- **Report:** always includes summary table + cost breakdown; escalated section conditional; Round 1 always shown, Round 2 only when present
- **Ledger:** `review-ledger.json` preserves full governance state per point

### Types (`types.py`)

- **ModelConfig.api_key:** resolved from `os.environ[api_key_env]` at access time
- **CostTracker:** `max_cost=None` disables guardrails; emits `§cost` structured log on `add()`

### Core Critical Constraints

1. **Lock release guarantee:** `_cleanup()` runs on success, exception, SIGTERM, and SIGINT — no exit path skips lock release
2. **Config-gates-execution:** validation errors block all command execution; no partial-config runs
3. **Parser purity:** `parser.py` is synchronous and side-effect-free; LLM-based recovery isolated in `normalization.py`
4. **Governance engagement mandate:** implicit acceptance (silence) and rote acceptance both escalate — author must demonstrate substantive reasoning
5. **Atomic persistence:** all review artifact writes use temp+fsync+rename to prevent partial writes
6. **Cost hard stop:** `CostLimitError` propagates through async orchestrator to CLI for clean exit

---

## Orchestrator

### Pipeline State Machine (`_common._run_adversarial_pipeline`)

- **States:** pre_author_response → round1_author_response → round2_exchange → governance → revision → completed. Exit state: cost_aborted.
- **Transitions:** Round 1 reviews + dedup complete → author response → (cost guardrail check; exceeded → cost_aborted with stub ledger, return None) → Round 2 → governance → revision if actionable findings, else completed. Revision failure downgrades ledger to "completed", does not abort.
- **Report ordering:** Report + ledger persisted BEFORE revision attempt; re-saved after if revision succeeds.

### Round 2 Exchange (`_common._run_round2_exchange`)

- **Skip conditions:** All groups ACCEPTED → no rebuttals sent. No CHALLENGE verdicts → no author final response solicited.
- **Reviewer scoping:** Rebuttal prompts sent only to reviewers who sourced contested groups (resolution != ACCEPTED). Context overflow excludes reviewer silently.
- **Author final failure:** Logged, skipped; governance proceeds using Round 1 positions.

### Governance Escalation (`_common._apply_governance_or_escalate`)

- **Catastrophic parse escalation:** <25% author response parse coverage → all groups escalated unconditionally.

### Cost Guardrail (`_common._check_cost_guardrail`)

- **Behavior:** 80% warning emitted once (flag-guarded). Exceeded → stub ledger (`points: []`, `total_points: 0`, `total_groups: 0`) saved, pipeline returns None.
- **Checkpoints:** After Round 1, after dedup, after author response.

### Reviewer Calls (`_common._call_reviewer`)

- **Return contract:** Always returns `list[ReviewPoint]`; empty on API failure (logged, skipped). Falls back to LLM normalization if parser yields zero points.

### Mode: Code Review (`code.py`)

- **Pre-flight:** Context window check filters reviewers; <1 active → None. Lock failure → None.
- **Dedup skip:** Triggered if any reviewer failed AND `len(active_reviewers) > 1`.
- **Revision artifact:** `revised-diff.patch`. Storage lock released in `finally`.

### Mode: Plan Review (`plan.py`)

- **Multi-file handling:** `input_files[0]` is primary; additional files wrapped as reference context with "do not review directly" instruction. Combined via `=== PRIMARY ARTIFACT ===` / `=== END PRIMARY ARTIFACT ===` delimiters.
- **Revision artifact:** `revised-plan.md`. Otherwise identical pipeline to code review.

### Mode: Integration Review (`integration.py`)

- **Role gate:** Requires `integration_reviewer` role; missing → None.
- **File discovery cascade:** Explicit `input_files` → `manifest.json` (status=="completed" tasks).
- **Spec discovery cascade:** Explicit → `000-strategic-summary.md` → `strategic-summary.md` → manifest → fallback text.
- **Single reviewer path:** No dedup; points promoted to groups 1:1. Context overflow → None (chunking deferred to v2).
- **Revision artifact:** `remediation-plan.md`.

### Mode: Spec Review (`spec.py`)

- **Non-adversarial:** No author role, no author response, no rebuttals, no governance, no escalation. Spec-specific system prompt and parser.
- **Summary shape:** `total_groups`, `total_points`, `multi_consensus` (>1 source), `single_source`.
- **Revision artifact:** `revised-spec-suggestions.md`. Revision failure non-fatal; report persisted before and after attempt.

### Intermediate Artifacts

- **Round 1:** `{reviewer}_raw.txt`, `{reviewer}_parsed.json`, `deduplication.json`
- **Round 2:** `author_raw.txt`, `author_responses.json`, `{reviewer}_rebuttal_raw.txt`, `{reviewer}_rebuttal_parsed.json`, `author_final_raw.txt`, `author_final_parsed.json`, `governance.json`
- **Final:** `dvad-report.md`, `review-ledger.json`, `original_content.txt`, `revised-{artifact}`

### Orchestrator Critical Constraints

1. **Lock safety:** Storage lock always released in `finally` block; lock failure is immediate None return.
2. **Formatting purity:** All `_formatting.py` functions are pure; author response concern text truncated to 120 chars for rebuttal display.
3. **Contested group definition:** Reviewer must be point source AND author resolution != ACCEPTED.
4. **Stub ledger invariant:** `_save_stub_ledger` always emits `points: []`, `total_points: 0`, `total_groups: 0` regardless of pipeline state at abort.
5. **Cost estimate coverage:** Accounts for Round 1 reviewers, dedup, author response, Round 2 rebuttals, author final, and revision.

---

## GUI Backend

### App Factory & Lifecycle (`gui/__init__.py`, `gui/app.py`)

- **Bootstrap:** `build_app(config_path)` assembles FastAPI with routes, static (`./static`), templates (`./templates`), Jinja filter `human_date` (ISO → `%-d %b %Y, %H:%M`).
- **CSRF:** `secrets.token_urlsafe(32)` generated once at startup, stored in `app.state.csrf_token`. All mutating endpoints validate `X-DVAD-Token` header match.
- **Shutdown:** `lifespan()` cancels any running review on app teardown.
- **Env factory:** `create_app_from_env()` reads config path from `DVAD_E2E_CONFIG` env var for `uvicorn --factory`.

### Review Lifecycle (`gui/api.py`, `gui/runner.py`)

- **State machine:** idle → running (`POST /review/start`) → complete | failed; `POST /review/{id}/cancel` → failed. 409 if review already running.
- **Concurrency:** Single-review enforcer via `ReviewRunner.current_task`; set to `None` on completion/failure/cancel.
- **Review ID:** Content hash of input files.
- **SSE streaming:** `GET /review/{id}/progress` — `asyncio.Queue(maxsize=500)`, drop-oldest on overflow, 15s keepalive ping. Buffered events for late-joining clients.
- **Logging intercept:** `storage.log` monkey-patched to emit `ProgressEvent`s during review execution.
- **Failure handling:** Stub ledger saved best-effort on failure/cancellation.

### Configuration API (`gui/api.py`)

- **Read/Write:** `GET /config` returns full config; `POST /config` saves. `_mutate_yaml_config` ensures atomic writes.
- **Granular mutations:** `POST /config/model-timeout` (10–7200), `/model-thinking` (boolean), `/model-max-tokens` (1–1000000, ≤ provider max), `/settings-toggle` (keys ∈ `{live_testing}`).
- **Env vars:** `GET/POST /config/env`, `PUT/DELETE /config/env/{name}`. Names: `^[A-Z_][A-Z0-9_]*$`; values reject `\r\n\0`, max 4096 chars. `.env` written `0o600`.
- **File uploads:** MAX_FILES=25, MAX_FILE_SIZE=10MB.

### Pages (`gui/pages.py`)

- **Dashboard:** Paginated (25/page), newest-first, 5s TTL cache. `show_test=false` filters projects containing "test" (case-insensitive).
- **Detail:** Redirects to `/` if ledger not found.
- **Config:** Vendor inference: explicit provider → `api_base` domain matching.

### Progress Classification (`gui/progress.py`)

- **`classify_log_message`:** Regex → `ProgressEvent(event_type, phase, detail)`. No match → `phase="unknown"`.
- **Cost events:** `message=""` (suppressed from console). Timestamps auto-generated UTC.

### Backend Critical Constraints

1. **CSRF enforcement:** Every mutating endpoint validates `X-DVAD-Token` before any state change.
2. **Single-review invariant:** `ReviewRunner` rejects concurrent starts with HTTP 409.
3. **Queue overflow policy:** Drop-oldest, not backpressure — clients may miss mid-stream events.
4. **Blocking I/O isolation:** All sync operations dispatched via `asyncio.to_thread`.
5. **Env file security:** `.env` at `0o600`; values validated against control chars and length.
6. **Atomic config writes:** No partial YAML on disk.

---

## GUI Frontend

### Review Submission Flow

- **State machine:** idle → interstitial_open (form submit, command preview) → review_submitting (POST `/api/review/start`) → redirect to detail page.
- **Mode gating:** Selected mode controls visible form fields; plan default-selected.
- **File inputs:** JSON arrays in hidden inputs. File picker modal persists selections across open/close cycles; supports `multiSelect` and `dirMode`.
- **Command preview:** `buildCommand()` assembles CLI string; interstitial overlay displays before execution.

### Live Review Progress (SSE)

- **Event ordering:** metadata event arrives first (role→model map, mode detection); cost events follow, cumulative per role.
- **Spec mode:** `_applySpecMode()` hides adversarial phases in both running and completed views.
- **Phase indicators:** active (accent+pulse), done (green), pending (outline). Pipeline rail: round1 → author → round2 → governance → overrides → revision.
- **Terminal:** SSE error/complete triggers delayed page reload (3s).

### Finding Cards & Overrides

- **Visual states:** escalated (yellow border), accepted (green), dismissed (grey), overridden (accent). Resolved cards at 50% opacity.
- **Override actions:** Three buttons per escalated card — Accept Reviewer, Accept Author, Keep Open.
- **Revision gate:** Button highlights only when all escalated groups resolved.

### Config Page — Role Assignment & Thinking

- **Role cardinality:** Radio for singular roles (author, dedup, normalization, revision, integration_reviewer); checkbox with max=2 for reviewer.
- **Thinking toggle:** Active only on models with role assignment (`thinking-eligible` class).
- **Inline editing:** Timeout/max-tokens fields. Blur/Enter commits; validation failure reverts.
- **YAML tabs:** Structured vs Raw views with validate/save.

### Config Page — API Keys & Settings

- **API key rows:** "present" badge when found in env file; raw input otherwise.
- **Settings toggle:** `.settings-on` (yellow+border) visual state.

### Frontend Critical Constraints

1. **SSE ordering invariant:** Metadata event must precede cost events; `_handleCostUpdate` depends on role→model map.
2. **Revision precondition:** All escalated groups must be resolved before revision can start — UI enforces by disabling button.
3. **Role cardinality enforcement:** Singular roles use radio behavior; reviewer uses checkbox with ceiling=2.
4. **Error recovery:** Network failures re-enable buttons + alert; HTTP errors parse JSON detail; SSE parse errors silently caught; inline edit failures revert.
