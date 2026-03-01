# Functional Specification: devils-advocate

## Table of Contents

1. [Core](#core)
2. [Orchestrator](#orchestrator)
3. [GUI](#gui)

---

# Core

## Identity & Entry

- **Binary:** `dvad` → `__main__.py` → `cli.cli()` (Click root)
- **Version:** 0.1.0

## CLI Commands

- **review:** Dispatches to `run_{plan,code,integration,spec}_review` orchestrator coroutines based on `mode` param
- **history:** List reviews or show single review detail by `review_id`
- **config:** Show/validate/init config (`init_config` creates `~/.config/devils-advocate/models.yaml`)
- **override:** Manual governance resolution; maps resolution strings through `resolution_map` before storage write
- **revise:** Post-review revision LLM call against stored review
- **gui:** Launch uvicorn web GUI; non-localhost requires `--allow-nonlocal`; port validated via real socket bind
- **install/uninstall:** systemd user service lifecycle for dvad-gui

## CLI Error Handling

- Config/validate/input failures → `sys.exit(1)`; KeyboardInterrupt → `_cleanup()` → `sys.exit(130)`; APIError|CostLimitError → `_cleanup()` → `sys.exit(1)`; SIGTERM → `_cleanup()`
- **`_cleanup()` invariant:** Always releases storage lock + closes file handle on every error path

## Configuration (`config.py`)

- **Resolution order:** explicit path > `./models.yaml` > `$DVAD_HOME/models.yaml` > `~/.config/devils-advocate/models.yaml`
- **Env loading:** `_load_dotenv` sets vars only if not already present (shell exports win)
- **Validation rules:** exactly 1 author, ≥2 reviewers, exactly 1 integration_reviewer, ≥1 dedup; dedup ≠ author
- **Defaults:** normalization → dedup model; revision → author model
- **Security:** config dir chmod 700, config file chmod 600
- **Failure:** missing `models` key or `roles` block → ConfigError

## Cost Tracking (`cost.py`)

- **Token estimation:** `len(text)//4`, minimum 1
- **Cost estimation:** returns 0.0 if model has no cost rates configured
- **Context window check:** fits=True always when `context_window` is None; threshold 80% of limit

## Deduplication (`dedup.py`)

- **Behavior:** LLM-based grouping of review points; mode="spec" uses distinct formatter/prompt/parser
- **Fallback:** Context overflow or empty input → each point promoted to singleton group (non-fatal)

## Governance Engine (`governance.py`)

- **Pure deterministic rule engine** — zero external dependencies beyond `types.py`
- **Rejection validation:** 3 regex criteria (technical term + mechanism + specific reference); default False → ambiguous rejection auto-accepts finding (favors reviewer)
- **Acceptance validation:** ≥15 words AND not rote phrase (16 hardcoded phrases); default False → escalate
- **Resolution matrix:** No response → ESCALATED; PARTIAL → always ESCALATED; unknown → ESCALATED
- **MAINTAINED:** ≥2 reviewers + valid rejection → ESCALATED; ≥2 + invalid → AUTO_ACCEPTED; single reviewer → ESCALATED
- **ACCEPTED:** challenged + no final response → ESCALATED; substantive rationale → AUTO_ACCEPTED; rote/thin → ESCALATED
- **REJECTED:** ≥2 + valid objection → ESCALATED; ≥2 + invalid → AUTO_ACCEPTED; single + unchallenged → AUTO_DISMISSED; single + challenged/integration → ESCALATED
- **Round precedence:** Final response supersedes Round 1 for challenged groups only

## ID Generation (`ids.py`)

- **Review ID:** `YYYYMMDDThhmmss_<sha256-6>_review`
- **Hierarchy:** group_id → point_id (child inherits parent prefix)
- **GUID resolution:** direct match → UUID regex extract → fuzzy Hamming ≤2 chars → None (handles LLM transcription errors)
- **Randomness:** `random.choice` (not cryptographically secure)

## Parsing (`parser.py`)

- **Strictly synchronous** — all parsers are pure functions
- **Thinking strip:** Removes `<thinking>`/`<reasoning>`/`**Thinking:**` blocks before parsing
- **Positional fallback:** author response only; rebuttal and final response require GUID match exclusively
- **Unknown resolution defaults:** author → UNKNOWN (escalated); rebuttal → CONCUR; final → MAINTAINED
- **Ungrouped points:** Always become singleton groups (no point ever discarded)
- **Temp IDs:** Review points get `temp_NNN`; final IDs assigned during dedup
- **Revised output extraction:** Requires exact canonical delimiters (`=== REVISED PLAN ===` etc.); missing → ""

## LLM Providers (`providers.py`)

- **Dispatch:** `call_model` routes by `model.provider` + `model.use_responses_api` to Anthropic/OpenAI-compatible/OpenAI-responses/MiniMax handlers
- **Output limits:** standard=16384, author=32000, revision=64000 tokens
- **Retry policy:** HTTP 529 → immediate fail; 429 → respect Retry-After; 5xx/timeout → exponential backoff+jitter; other 4xx → immediate fail; max 3 retries
- **Anthropic specifics:** Strips `<thinking>` blocks; opus-4-6/sonnet-4-6 use adaptive thinking; others use budget_tokens (added to max_tokens)
- **OpenAI specifics:** o3/o4 use `max_completion_tokens`; reasoning_effort=medium for spec, high otherwise
- **Zero visible content with non-zero output tokens → warning (not exception)**

## Revision (`revision.py`)

- **Actionable resolutions:** {auto_accepted, accepted, overridden} — only these produce revision input
- **Skip conditions:** No actionable findings → skip (plan/code/integration); spec revision unconditional (ignores governance)
- **Extraction:** Strict canonical delimiters; missing → ""
- **Context window exceeded → log + return ""**

## Storage (`storage.py`, class StorageManager)

- **Data dir:** `$DVAD_HOME` or `~/.local/share/devils-advocate/`
- **Locking:** `O_CREAT|O_EXCL`; stale detection: age >3600s or dead PID → remove + retry (3 attempts)
- **Write durability:** All writes via mkstemp → fsync → os.replace (no partial writes visible)
- **Lock file content:** `{pid, hostname, timestamp}` JSON
- **Logging:** Lazy-open append with immediate flush; defaults to `session.log` before `set_review_id`

## Type System (`types.py`)

- **Severity:** CRITICAL > HIGH > MEDIUM > LOW > INFO
- **Resolution lifecycle:** PENDING → {ACCEPTED, REJECTED, PARTIAL} → governance → {AUTO_ACCEPTED, AUTO_DISMISSED, ESCALATED} → manual → OVERRIDDEN
- **ModelConfig.api_key:** Live read from `os.environ` on every property access (never cached)
- **CostTracker:** Mutates in-place; emits `§cost` log events; tracks per-role and per-model costs; warns at 80%, errors at limit
- **ReviewContext:** Auto-generates 4-char `id_suffix` in `__post_init__`

## Prompts (`prompts.py`)

- **Template loading:** `importlib.resources` from `templates/*.txt`; `str.format(**kwargs)`
- **Failure:** Missing template or variable → AdvocateError (not FileNotFoundError/KeyError)
- **System prompts:** Module-level cached after first load (lazy singleton)

## Service Management (`service.py`)

- **Platform gate:** Linux only; non-Linux returns error string
- **Binary discovery:** venv sibling first, then PATH
- **Service:** `dvad-gui.service`; KillSignal=SIGINT; Restart=on-failure; RestartSec=5
- **Defensive:** `is_active`/`is_enabled` swallow all exceptions → return False

## Critical Constraints

1. **Governance safety defaults:** Ambiguous rejection → auto-accept finding (favors reviewer); ambiguous acceptance → escalate (favors human review)
2. **Write atomicity:** Every persistent write uses mkstemp+fsync+os.replace — crash-safe
3. **Env isolation:** Shell exports always override dotenv; ModelConfig re-reads env on every access
4. **Security posture:** Config dir 700, config file 600, env file written with umask 0o077
5. **Cleanup guarantee:** `_cleanup()` (lock release + file close) executes on all CLI error/signal paths

---

# Orchestrator

## Pipeline Architecture

- **Four modes:** plan, code, integration, spec — each with dedicated orchestrator module
- **plan.py and code.py are structurally identical** (differ only in mode string + revision filename)
- **integration.py:** Single reviewer, no parallel phase, per-point group promotion (no dedup merging)
- **spec.py:** Collaborative ideation — never calls `_run_adversarial_pipeline`; no Round 2, no author, no rebuttals, no governance

## Adversarial Pipeline (`_common._run_adversarial_pipeline`)

- **Sequence:** Author Round 1 → Round 2 exchange → governance → save → revision
- **Author context overflow → return None**
- **Cost exceeded post-author → stub ledger + None**
- **All accepted by author → skip Round 2 entirely**
- **No CHALLENGE verdicts → skip author final response**
- **Parse coverage <25% → escalate ALL groups** (hard-coded threshold, not configurable)
- **No actionable governance decisions → skip revision**
- **Revision failure → downgrade to warning, review still completes**
- **Rebuttal dispatch:** Only to reviewers whose groups are contested AND fit context window; `asyncio.gather(return_exceptions=True)` — individual failures captured, not propagated
- **Author final response exception → warning; review proceeds on Round 1 positions**

## Round 2 Exchange (`_common._run_round2_exchange`)

- **Skip conditions:** Author accepted all groups → no Round 2; no CHALLENGE verdicts after rebuttals → no author final
- **Contested groups:** Filtered per-reviewer (reviewer must be source AND author did not fully accept)

## Code Review (`orchestrator/code.py`)

- **Flow:** Read file → review_id from content hash → parallel `_call_reviewer` via `asyncio.gather` → dedup → adversarial pipeline
- **Dedup skip:** If any reviewer failed AND >1 reviewer configured → silent 1:1 promotion (no cross-model dedup)
- **Lock always released in finally; `storage.close()` in finally**
- **spec_content=None (not "") when no spec file**
- **Revision output:** `revised-diff.patch`

## Plan Review (`orchestrator/plan.py`)

- **Input convention:** `input_files[0]` = primary (reviewed); `input_files[1:]` = reference context with explicit "do not review" instruction
- **review_id generated from full assembled content (not primary file alone)**
- **Revision output:** `revised-plan.md`

## Integration Review (`orchestrator/integration.py`)

- **Reviewer:** Single `integration_reviewer` role; no parallel phase
- **Spec discovery cascade:** explicit > `project_dir/000-strategic-summary.md` > `project_dir/strategic-summary.md` > manifest-dir fallback
- **File discovery:** explicit `input_files` > manifest `tasks[status==completed].files`
- **Content assembly:** Files joined with `--- {path} ---`/`--- END {path} ---` delimiters
- **Oversized content → None (chunking explicitly deferred)**
- **Each point gets its own group (no dedup merging)**
- **Revision output:** `remediation-plan.md`

## Spec Review (`orchestrator/spec.py`)

- **No adversarial fields:** `author_model=""`, `author_responses=[]`, `governance_decisions=[]`, `rebuttals=[]`, `author_final_responses=[]`
- **Consensus counting:** multi_consensus = groups with >1 source reviewer; single_source = 1
- **Revision:** `run_spec_revision` called unconditionally; failure non-fatal; report re-saved only if `revised_output` is truthy
- **Revision output:** `revised-spec-suggestions.md`

## Display (`_display.py`)

- **Cost estimation:** Uses `min(input_tokens, MAX_OUTPUT_TOKENS)` as estimated output for both rounds
- **Governance colors:** auto_accepted=green, escalated=yellow, auto_dismissed=cyan, others=red
- **Summary table:** Only rows with count > 0

## Critical Constraints

1. **Lock lifecycle:** Acquired before Round 1; released in finally block in all four modes
2. **Dedup bypass:** Any reviewer failure with >1 reviewer → silent skip of cross-model dedup (all three adversarial modes)
3. **25% parse floor:** Hard-coded; below threshold all groups escalated unconditionally
4. **Cost guardrail:** 80% warning emitted exactly once (flag reset after print); exceeded → stub + abort
5. **Stub ledger:** `_save_stub_ledger` always produces structurally valid ledger with all required keys for terminal/non-success states

---

# GUI

## Application Bootstrap (`app.py`, `__init__.py`)

- **Factory:** `create_app(config_path)` → `build_app`; `create_app_from_env` reads `DVAD_E2E_CONFIG` env var for uvicorn `--factory`
- **CSRF:** `secrets.token_urlsafe(32)` generated once at startup; fixed for process lifetime; all mutating endpoints require `X-DVAD-Token` header match → 403
- **Singleton runner:** One `ReviewRunner` shared via `app.state` across all requests
- **Shutdown:** `lifespan` asynccontextmanager cancels `current_task` on exit
- **Template filter:** `human_date` converts ISO → `%-d %b %Y, %H:%M`

## Review Runner (`runner.py`)

- **Concurrency:** One review at a time globally; `start_review` raises HTTP 409 if `current_task` not done
- **Background task flow:** load_config → StorageManager → persist manifest → copy uploads → monkey-patch `storage.log` → classify → emit_event → dispatch to orchestrator → terminal event
- **Event queue:** `asyncio.Queue(maxsize=500)`; overflow drops oldest, retries once, silent drop on second fail
- **State per review:** `{queue, buffered, state, created_at, last_event_at}` — grows unbounded (no TTL eviction)
- **Cancellation:** `CancelledError` → attempt `_save_stub_ledger` → re-raise (preserves asyncio cancellation)
- **Generic exception:** Attempt stub ledger → terminal error event → swallowed (not re-raised)
- **Finally:** Always clears `current_review_id=None`, `current_task=None`

## API Endpoints (`api.py`)

- **Review lifecycle:** start (POST), cancel (POST), progress SSE (GET), detail JSON (GET), override (POST), revise (POST), log (GET), report download (GET), revised download (GET)
- **Config mutation:** model timeout/thinking/max_tokens, settings toggle (only `live_testing` accepted), validate, save
- **Env var management:** GET/PUT/DELETE/POST; name regex `^[A-Z_][A-Z0-9_]*$`; no `\r\n\0`; max 4096 chars; restricted to `api_key_env` values in config
- **Filesystem browser:** GET `/api/fs/ls`; silently skips dotfiles
- **Upload limits:** max 10MB per file; max 25 files
- **Override validation:** Only `{overridden, auto_dismissed, escalated}` accepted; invalidates page cache (`_review_cache["data"]=None`)
- **SSE protocol:** `asyncio.wait_for(queue.get(), timeout=15.0)`; timeout → `": ping\n\n"`; terminal event closes stream
- **Env file security:** umask 0o077, chmod 0o600; mutates `os.environ` in-process
- **Blocking config reads:** Wrapped in `asyncio.to_thread`

## Pages (`pages.py`)

- **Dashboard:** Reviews sorted newest-first; "test" projects hidden by default (case-insensitive filter); 25/page; page clamped to `[1, total_pages]`
- **Review detail:** Checks runner status first; if not running → load from storage; missing ledger → 302 redirect to `/`
- **Config page:** Models grouped by vendor sorted by `cost_per_1k_output` desc; vendor inferred from `api_base` substrings (OpenAI/xAI/Google/DeepSeek/Moonshot/MiniMax; fallback=`provider.title()`)
- **Cache:** `_review_cache` with 5-second TTL; invalidated on override
- **has_original:** Requires `original_content.txt` file existence (precondition for revise button)
- **Unknown resolution → escalated bucket (defensive default)**

## Progress Events (`progress.py`)

- **Event types:** log, phase, cost, complete, error, metadata
- **Classification:** `_PHASE_PATTERNS` ordered list; first match wins; `§cost` pattern must be first
- **Cost events:** `message=""` (suppressed from console); structured `detail={role, model, cost, total}`
- **Unmatched log → event_type="log", phase="unknown"**
- **Terminal:** phase="done" (success) or phase="error" (failure)

## Critical Constraints

1. **Single-review concurrency:** Enforced at runner level; HTTP 409 on conflict; no queuing
2. **CSRF on all mutations:** `X-DVAD-Token` header must match startup-generated token; read-only endpoints exempt
3. **Unbounded growth:** Both `statuses` dict and per-review `buffered` events list grow without eviction
4. **Env var restriction:** Only names matching `api_key_env` values from config may be written (prevents arbitrary env mutation)
5. **SSE keepalive:** 15-second ping cycle prevents proxy/client timeout; terminal event is authoritative stream closer
