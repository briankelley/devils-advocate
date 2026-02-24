# Devil's Advocate (dvad) -- Test Coverage Gap Analysis

**Date:** 2026-02-24
**Scope:** All source modules under `src/devils_advocate/` vs all test files under `tests/`
**Method:** Manual audit of every exported function, class, and method against existing test assertions

---

## Executive Summary

The dvad project has **solid test coverage for its deterministic, pure-function modules** (config, cost, governance, ids, parser, storage) but has **virtually zero coverage for its async orchestration layer, CLI interface, report generation, and LLM provider integration**. These untested modules represent the majority of the runtime code path and contain the most complex branching logic, error handling, and failure recovery.

**By the numbers:**

| Coverage Level | Module Count | Notes |
|---|---|---|
| Good (direct, thorough tests) | 8 | config, cost, governance, ids, normalization, storage, gui/progress, revision |
| Partial (some paths tested) | 5 | parser, prompts, gui/api, gui/pages, gui/runner |
| None (zero direct tests) | 12 | providers, cli, output, dedup, orchestrator/_common, _display, _formatting, plan (mostly), code, integration, spec, ui |

---

## Module-by-Module Gap Analysis

---

### 1. `providers.py` -- LLM API Provider Layer

**Path:** `src/devils_advocate/providers.py`
**Current Coverage:** ~7% (indirect only, via mocked calls in test_normalization and test_orchestrator)
**Priority:** CRITICAL

This is the most dangerous coverage gap in the project. Every API call flows through this module, and it contains retry logic, error classification, rate limiting, and provider-specific request construction.

#### Functions with ZERO direct tests:

| Function | Lines | Risk | Notes |
|---|---|---|---|
| `call_anthropic()` | 42-86 | Critical | Anthropic Messages API construction, thinking/reasoning mode branching (adaptive for opus-4-6/sonnet-4-6, budget-based for others), response content block iteration |
| `call_openai_compatible()` | 89-149 | Critical | `max_completion_tokens` vs `max_tokens` branching for o3/o4 models, `reasoning_effort` for openai.com, `thinking` for moonshot, empty-content warning logging path |
| `call_minimax()` | 152-193 | High | MiniMax native API construction, `reasoning_split` parameter |
| `call_model()` | 199-213 | High | Provider routing dispatcher (anthropic/minimax/openai-compatible fallback) |
| `call_with_retry()` | 219-271 | Critical | Exponential backoff, jitter, Retry-After header parsing, 529 overload abort, 429 rate limit, 5xx retry, timeout/connect error retry, hint logging on first timeout |

#### Untested critical paths:
- **Anthropic thinking mode branching:** adaptive vs budget-based based on model_id substring matching
- **OpenAI reasoning_effort:** conditional on api_base containing "api.openai.com"
- **Empty content warning:** when output_tokens > 0 but text is empty (reasoning consumed budget)
- **529 overload:** immediate abort with APIError (different from 429 retry)
- **Retry-After header:** float parsing and max() with exponential backoff
- **Timeout hint logging:** only on first attempt (attempt == 0)
- **Max retries exhausted:** final APIError raise with last exception chaining

#### Tests needed:
- Unit tests for each provider function with httpx response mocking (respx)
- Parameterized tests for thinking/reasoning mode branches per provider
- Retry engine tests: 429 with Retry-After, 529 abort, 5xx backoff, timeout recovery
- Edge case: empty content with output tokens (logging path)
- Edge case: max retries exhausted raises APIError

---

### 2. `cli.py` -- Click CLI Interface

**Path:** `src/devils_advocate/cli.py`
**Current Coverage:** None
**Priority:** HIGH

The entire CLI -- the primary user interface -- has zero tests.

#### Functions with ZERO direct tests:

| Function | Lines | Risk | Notes |
|---|---|---|---|
| `cli()` | 38-42 | Low | Click group, trivial |
| `review()` | 47-194 | Critical | Config loading, validation, mode routing, signal handling, asyncio loop management, cleanup on KeyboardInterrupt/APIError/CostLimitError |
| `history()` | 200-259 | Medium | Review listing, detail display, markdown rendering |
| `config_cmd()` | 265-351 | Medium | --show/--init routing, table display, validation output |
| `override()` | 356-417 | Medium | Resolution mapping, StorageManager interaction |
| `revise()` | 423-542 | High | Ledger loading, original content resolution, asyncio loop, error handling |
| `gui_cmd()` | 548-594 | High | Port bind preflight, nonlocal safety check, uvicorn launch |

#### Untested critical paths:
- `review()` signal handling: SIGTERM via `loop.add_signal_handler()`, fallback to `signal.signal()` on Windows
- `review()` KeyboardInterrupt cleanup path
- `review()` APIError and CostLimitError exit paths
- `review()` mode routing: plan/code/integration/spec branching
- `review()` config validation: error vs warning handling
- `gui_cmd()` port-in-use detection and error message
- `gui_cmd()` nonlocal binding refusal without `--allow-nonlocal`
- `revise()` missing original_content.txt error path
- `revise()` revision failure (generic Exception) non-fatal path

#### Tests needed:
- Click CliRunner-based tests for each command
- Config error propagation tests
- Signal handling validation (mocked)
- Mode routing verification
- GUI port bind conflict detection

---

### 3. `output.py` -- Report and Ledger Generators

**Path:** `src/devils_advocate/output.py`
**Current Coverage:** None
**Priority:** HIGH

Report and ledger generation is the final output users see. Zero tests exist despite significant formatting logic and branching.

#### Functions with ZERO direct tests:

| Function | Lines | Risk | Notes |
|---|---|---|---|
| `_build_lookup_maps()` | 14-22 | Medium | Creates decision/response/rebuttal/final lookup dicts |
| `generate_report()` | 28-120 | High | Full report generation with mode dispatch (spec), summary table, escalated/non-escalated sections, revised output formatting per mode (plan/integration/code), cost breakdown |
| `_format_group_section()` | 126-206 | High | Individual group formatting: Round 1 author response (present vs missing), Round 2 rebuttals (CONCUR vs CHALLENGE), author final response (only for challenged groups), governance reason |
| `generate_ledger()` | 212-271 | High | JSON ledger structure with point/group/response/rebuttal/governance aggregation, cost breakdown including role_costs |
| `_generate_spec_report()` | 277-368 | High | Spec-specific report: theme grouping, consensus indicators, high-consensus section, alphabetical theme sorting with "Other" last |

#### Untested critical paths:
- `generate_report()` mode dispatch: spec vs non-spec
- `generate_report()` revised output label selection: plan/integration/code
- `generate_report()` escalated items section (conditional)
- `_format_group_section()` missing author response fallback text
- `_format_group_section()` rebuttal display: CONCUR icon "+" vs CHALLENGE icon "x"
- `_format_group_section()` author final response: shown only when challenges exist
- `_generate_spec_report()` theme grouping and sorting (alphabetical, "Other" last)
- `_generate_spec_report()` consensus indicator display (multi-reviewer vs single)
- `_generate_spec_report()` high-consensus section (conditional on multi-source groups)
- `generate_ledger()` role_costs inclusion in cost dict

#### Tests needed:
- Unit tests with constructed ReviewResult objects for each mode
- Edge cases: zero groups, all escalated, no revised output, spec mode with consensus
- Ledger structure validation: required keys, cost breakdown accuracy
- Group section formatting: all combinations of author response presence/absence, rebuttal verdicts, governance decisions

---

### 4. `dedup.py` -- Deduplication Engine

**Path:** `src/devils_advocate/dedup.py`
**Current Coverage:** None (indirect only via test_orchestrator.py)
**Priority:** HIGH

#### Functions with ZERO direct tests:

| Function | Lines | Risk | Notes |
|---|---|---|---|
| `format_points_for_dedup()` | 14-27 | Medium | Point formatting with optional LOCATION field |
| `format_suggestions_for_dedup()` | 30-41 | Medium | Spec-mode suggestion formatting with THEME/CONTEXT fields |
| `deduplicate_points()` | 44-116 | High | Async orchestration: empty points early return, spec vs non-spec mode branching, context window overflow fallback (each point becomes own group), LLM call, cost tracking, response parsing dispatch |

#### Untested critical paths:
- `deduplicate_points()` empty points returns `[]`
- `deduplicate_points()` context window overflow fallback: creates singleton groups
- `deduplicate_points()` spec mode: different formatting, prompt, and parser
- `format_points_for_dedup()` optional LOCATION field inclusion/exclusion
- `format_suggestions_for_dedup()` optional CONTEXT field

#### Tests needed:
- Unit tests for both formatting functions
- Async tests for `deduplicate_points()` with respx mocking
- Context window overflow fallback path
- Spec mode vs non-spec mode branching
- Empty input edge case

---

### 5. `orchestrator/_common.py` -- Shared Adversarial Pipeline

**Path:** `src/devils_advocate/orchestrator/_common.py`
**Current Coverage:** None (partially exercised by test_orchestrator.py's run_plan_review tests)
**Priority:** CRITICAL

This is the core engine. Every adversarial review mode (plan, code, integration) routes through `_run_adversarial_pipeline()`.

#### Functions with ZERO direct tests:

| Function | Lines | Risk | Notes |
|---|---|---|---|
| `_promote_points_to_groups()` | 75-96 | Medium | Dedup fallback: each point becomes own group |
| `_call_reviewer()` | 102-170 | Critical | Reviewer call with custom system_prompt and point_parser support, normalization fallback on parse failure |
| `_run_round2_exchange()` | 176-385 | Critical | Per-reviewer contested group filtering, rebuttal phase with context window check, author final response (only if challenges), APIError handling for author final |
| `_apply_governance_or_escalate()` | 391-423 | High | Catastrophic parse failure detection (<25% coverage), escalation fallback |
| `_check_cost_guardrail()` | 429-458 | Medium | 80% warning emission, exceeded flag check, warned_80 reset |
| `PipelineInputs` | 464-484 | Low | Dataclass, trivial |
| `_run_adversarial_pipeline()` | 487-715 | Critical | Full pipeline: author response, Round 2, governance, report/ledger save, revision (conditional on actionable findings), original content persistence |

#### Untested critical paths:
- `_call_reviewer()` normalization fallback when parse_review_response returns empty
- `_call_reviewer()` custom system_prompt and point_parser (spec mode)
- `_run_round2_exchange()` all-accepted shortcut (skips rebuttals)
- `_run_round2_exchange()` per-reviewer contested group filtering
- `_run_round2_exchange()` context window exceeded during rebuttal (skip reviewer)
- `_run_round2_exchange()` author final prompt context window exceeded (fall through)
- `_run_round2_exchange()` author final APIError (graceful degradation)
- `_run_round2_exchange()` no challenges (skip author final)
- `_apply_governance_or_escalate()` catastrophic parse failure at <25%
- `_check_cost_guardrail()` 80% warning emission and reset
- `_check_cost_guardrail()` exceeded flag returns True
- `_run_adversarial_pipeline()` author prompt context window exceeded
- `_run_adversarial_pipeline()` no actionable findings (skip revision)
- `_run_adversarial_pipeline()` revision failure (non-fatal Exception)

#### Tests needed:
- Isolated unit tests for `_promote_points_to_groups()`, `_apply_governance_or_escalate()`, `_check_cost_guardrail()`
- Async integration tests for `_call_reviewer()` with respx mocking
- End-to-end pipeline tests for `_run_adversarial_pipeline()` covering:
  - Full happy path
  - All-accepted shortcut
  - Catastrophic parse failure
  - Cost guardrail abort
  - Revision skip (no actionable findings)
  - Revision failure (non-fatal)

---

### 6. `orchestrator/_display.py` -- Display Helpers

**Path:** `src/devils_advocate/orchestrator/_display.py`
**Current Coverage:** None
**Priority:** MEDIUM

#### Functions with ZERO direct tests:

| Function | Lines | Risk | Notes |
|---|---|---|---|
| `_estimate_total_cost()` | 26-52 | Medium | Cost estimation covering both rounds; uses revision_model fallback to author |
| `_print_dry_run()` | 55-158 | Low | Console table output, no return value to assert |
| `_print_summary_table()` | 160-185 | Low | Console table output |
| `_print_governance_summary()` | 188-203 | Low | Console per-resolution count |

#### Tests needed:
- `_estimate_total_cost()` unit tests with known model configs verifying cost calculation
- Display functions could be tested by capturing Rich console output (lower priority)

---

### 7. `orchestrator/_formatting.py` -- Prompt Formatting Helpers

**Path:** `src/devils_advocate/orchestrator/_formatting.py`
**Current Coverage:** None
**Priority:** HIGH

These functions produce the text that LLMs receive. Incorrect formatting leads to incorrect LLM behavior.

#### Functions with ZERO direct tests:

| Function | Lines | Risk | Notes |
|---|---|---|---|
| `_format_groups_for_author()` | 20-41 | High | GUID embedding, reviewer count grammar ("1 reviewer" vs "2 reviewers"), feedback nesting with recommendation/location |
| `_format_author_responses_for_rebuttal()` | 44-60 | High | Response map lookup, "[NO AUTHOR RESPONSE]" fallback |
| `_get_contested_groups_for_reviewer()` | 63-80 | High | Filtering logic: only groups where reviewer was source AND author did not accept |
| `_format_challenged_groups()` | 83-125 | High | Challenge-only filtering, multi-section formatting with original findings, Round 1 response, reviewer challenges |
| `_group_to_dict()` | 128-140 | Medium | Serialization with optional guid field |
| `_compute_summary()` | 143-155 | Medium | Governance decision counting |

#### Tests needed:
- Unit tests for each function with constructed ReviewGroup/AuthorResponse/RebuttalResponse objects
- `_get_contested_groups_for_reviewer()` filtering logic: accepted excluded, rejected/partial/no_response included, reviewer not in source_reviewers excluded
- `_format_groups_for_author()` GUID embedding and reviewer count grammar
- `_format_challenged_groups()` groups with no challenges produce empty string
- `_compute_summary()` governance resolution counting accuracy

---

### 8. `orchestrator/code.py` -- Code Review Orchestrator

**Path:** `src/devils_advocate/orchestrator/code.py`
**Current Coverage:** None
**Priority:** HIGH

#### Functions with ZERO direct tests:

| Function | Lines | Risk | Notes |
|---|---|---|---|
| `run_code_review()` | 39-230 | High | Full code review: spec file support, pre-flight context window checks, parallel reviewer calls with exception handling, dedup skip on partial failure, cost estimate pre-check, lock management |

#### Untested critical paths:
- Spec file reading and prompt inclusion
- Reviewer context window skip with `active_reviewers` accumulator
- No reviewers available exit
- Parallel reviewer exception handling (gather with return_exceptions)
- Dedup skip on partial reviewer failure
- Cost estimate exceeding max_cost
- Lock acquisition failure

#### Tests needed:
- Async tests with respx mocking (similar pattern to existing test_orchestrator.py tests for plan mode)
- Edge case: all reviewers exceed context window
- Edge case: partial reviewer failure triggers dedup skip

---

### 9. `orchestrator/integration.py` -- Integration Review Orchestrator

**Path:** `src/devils_advocate/orchestrator/integration.py`
**Current Coverage:** None
**Priority:** HIGH

#### Functions with ZERO direct tests:

| Function | Lines | Risk | Notes |
|---|---|---|---|
| `run_integration_review()` | 35-247 | High | File discovery (input_files, manifest, strategic summaries), context window check, single-reviewer pipeline (each point is own group), lock management |

#### Untested critical paths:
- File discovery from manifest.json (task status filtering, file existence check)
- Spec discovery from `000-strategic-summary.md` or `strategic-summary.md`
- No manifest and no input files error
- No files to review error
- Context window exceeded for combined content
- Single-reviewer group creation (no dedup phase)

#### Tests needed:
- Async tests with file system fixtures and respx mocking
- Manifest-based file discovery
- Strategic summary resolution priority

---

### 10. `orchestrator/spec.py` -- Spec Review Orchestrator

**Path:** `src/devils_advocate/orchestrator/spec.py`
**Current Coverage:** None
**Priority:** HIGH

#### Functions with ZERO direct tests:

| Function | Lines | Risk | Notes |
|---|---|---|---|
| `run_spec_review()` | 52-363 | High | Non-adversarial pipeline (no governance), multi-file reference context construction, custom parser and system prompt delegation, consensus counting, revision with fallback, report save-then-update pattern |
| `_estimate_spec_cost()` | 369-383 | Medium | Simpler cost estimate (no Round 2) |
| `_print_spec_dry_run()` | 386-446 | Low | Console table output |
| `_print_spec_summary_table()` | 449-468 | Low | Console table output |

#### Untested critical paths:
- Multi-file reference context construction (primary + reference files)
- Custom parser (`parse_spec_response`) and system prompt delegation
- No suggestions from any reviewer
- Dedup skip on partial reviewer failure
- Cost guardrail checkpoints (multiple)
- Report save before revision, then re-save after revision
- Revision failure (non-fatal exception)

#### Tests needed:
- Async tests with respx mocking covering the non-adversarial pipeline
- Multi-file reference context construction
- Revision failure graceful degradation

---

### 11. `orchestrator/plan.py` -- Plan Review Orchestrator

**Path:** `src/devils_advocate/orchestrator/plan.py`
**Current Coverage:** Partial (4 tests in test_orchestrator.py)
**Priority:** MEDIUM

The existing tests cover the happy path (successful review), dry run, context window exceeded, and cost limit exceeded. However, several paths remain untested.

#### Untested paths:
- Multi-file reference context construction (files beyond the first input)
- Partial reviewer failure handling (some succeed, some fail)
- Dedup skip due to partial failure
- All reviewers fail (no points produced)
- Lock acquisition failure
- Cost guardrail abort mid-pipeline

#### Tests needed:
- Multi-file reference context test
- Partial reviewer failure with dedup skip
- Lock contention handling

---

### 12. `parser.py` -- Response Parsing

**Path:** `src/devils_advocate/parser.py`
**Current Coverage:** Partial (23 tests across 7 test classes)
**Priority:** MEDIUM

Well-tested for the primary functions but missing coverage for spec-mode parsers and internal helpers.

#### Functions with ZERO direct tests:

| Function | Lines | Risk | Notes |
|---|---|---|---|
| `_normalize_severity()` | ~ | Low | Normalization map lookup, default "medium" |
| `_normalize_category()` | ~ | Low | Normalization map lookup, default "other" |
| `_normalize_theme()` | ~ | Medium | Theme map lookup, used by spec mode |
| `_extract_multiline_field()` | ~ | Medium | Shared extraction logic for multiline fields |
| `_parse_grouped_response()` | ~ | High | Core shared parser for grouped responses (used by author and final parsers) |
| `parse_spec_response()` | ~ | High | Spec-mode suggestion parsing with THEME/CONTEXT fields |
| `parse_spec_dedup_response()` | ~ | High | Spec-mode dedup response parsing with consensus indicators |

#### Partially tested functions:
- `parse_review_response()`: 4 tests -- missing edge cases for all severity/category normalization variants
- `parse_author_response()`: 3 tests -- missing partial GUID match, multi-group with mixed GUIDs
- `parse_rebuttal_response()`: 2 tests -- missing unknown verdict handling, multiple reviewers
- `parse_author_final_response()`: 2 tests -- missing MAINTAINED vs other resolutions edge cases

#### Tests needed:
- Spec-mode parser tests: `parse_spec_response()`, `parse_spec_dedup_response()`
- Normalization map tests: parameterized tests for all aliases in `_SEVERITY_MAP`, `_CATEGORY_MAP`, `_THEME_MAP`
- `_extract_multiline_field()` edge cases
- `_parse_grouped_response()` shared logic

---

### 13. `prompts.py` -- Prompt Template Builders

**Path:** `src/devils_advocate/prompts.py`
**Current Coverage:** Partial (10 tests for 2 of 13 functions)
**Priority:** MEDIUM

Only `build_round1_author_prompt()` and `build_author_final_prompt()` are tested. The remaining 11 builder functions have zero tests.

#### Functions with ZERO direct tests:

| Function | Lines | Risk | Notes |
|---|---|---|---|
| `load_template()` | 24-39 | Medium | Template loading with missing file error, missing variable error |
| `get_reviewer_system_prompt()` | 45-50 | Low | Lazy-loaded cached system prompt |
| `build_review_prompt()` | 63-82 | High | Round 1 reviewer prompt with spec block (conditional), mode_label selection |
| `build_reviewer_rebuttal_prompt()` | 109-123 | High | Round 2 rebuttal prompt with mode interpolation |
| `build_dedup_prompt()` | 144-146 | Medium | Dedup instruction prompt |
| `build_normalization_prompt()` | 149-151 | Medium | Normalization instruction prompt |
| `build_integration_prompt()` | 154-160 | Medium | Integration reviewer prompt |
| `get_spec_reviewer_system_prompt()` | 168-173 | Low | Lazy-loaded spec system prompt |
| `build_spec_review_prompt()` | 176-178 | Medium | Spec reviewer instruction prompt |
| `build_spec_dedup_prompt()` | 181-183 | Medium | Spec dedup instruction prompt |
| `build_spec_revision_prompt()` | 186-195 | Medium | Spec revision instruction prompt |

#### Tests needed:
- `load_template()` error handling: missing template, missing variable
- All untested builder functions: verify template loads without error, correct variable substitution
- `build_review_prompt()` with and without spec content
- Spec-mode prompt builders: all four

---

### 14. `revision.py` -- Revision Engine

**Path:** `src/devils_advocate/revision.py`
**Current Coverage:** Good (18 tests)
**Priority:** LOW

Well-covered by test_revision.py.

#### Remaining gaps:
- `build_spec_revision_context()` -- no direct test (called by `run_spec_revision()`)
- `run_spec_revision()` -- no async test (only `run_revision()` is tested)
- `_run_revision_core()` context window exceeded path
- `_run_revision_core()` empty extracted output path

#### Tests needed:
- `build_spec_revision_context()` unit test
- `run_spec_revision()` async test with respx
- `_run_revision_core()` context window and empty output edge cases

---

### 15. `types.py` -- Type Definitions

**Path:** `src/devils_advocate/types.py`
**Current Coverage:** Partial (indirect through test_cost.py, test_governance.py, etc.)
**Priority:** LOW

#### Untested components:
- `CostTracker._log_fn` emission during `add()`
- `CostTracker.role_costs` tracking
- `CostTracker.warned_80` flag behavior
- `CostTracker.exceeded` flag behavior
- `ReviewContext.make_group_id()` -- only tested indirectly via parser/dedup
- `ReviewContext.make_point_id()` -- only tested indirectly

#### Tests needed:
- `CostTracker` log_fn callback tests
- `CostTracker` role_costs accumulation
- `CostTracker` warned_80 and exceeded flag triggers (partially covered in test_cost.py)

---

### 16. `ui.py` -- Console Output

**Path:** `src/devils_advocate/ui.py`
**Current Coverage:** None
**Priority:** LOW

Contains only `console = Console(stderr=True)` and `print_panel()`. The `console` is a Rich Console singleton; `print_panel()` is a convenience wrapper.

#### Tests needed:
- Minimal: verify `print_panel()` does not raise (smoke test)

---

### 17. GUI Modules

#### `gui/__init__.py` + `gui/app.py`
**Coverage:** Indirect (via test_gui_routes.py)
**Priority:** LOW -- adequately covered through route tests

#### `gui/api.py`
**Coverage:** Partial (via test_gui_api.py + test_gui_routes.py)
**Priority:** MEDIUM

**Untested paths:**
- `_get_git_info()` -- git branch/commit detection
- `_mutate_yaml_config()` success path (all tests only validate rejection, not successful mutation)
- Review start success path (actual orchestrator launch)
- Config save success path
- Config validate with complex YAML structures
- SSE progress for active review (only nonexistent review tested)

#### `gui/_helpers.py`
**Coverage:** Indirect
**Priority:** LOW -- trivial module (`get_gui_storage()`)

#### `gui/pages.py`
**Coverage:** Partial (via test_gui_pages.py + test_gui_routes.py)
**Priority:** LOW

**Untested paths:**
- `_list_reviews_cached()` with actual review data
- Dashboard with reviews present
- Review detail page with full review data

#### `gui/progress.py`
**Coverage:** Good (22 tests in test_gui_progress.py)
**Priority:** LOW -- well covered

#### `gui/runner.py`
**Coverage:** Partial (9 tests in test_gui_runner.py)
**Priority:** MEDIUM

**Untested paths:**
- `start_review()` -- actual review launch (subprocess spawning)
- `_run()` -- the core async runner logic
- `cancel_review()` -- process termination

---

## Priority Matrix

### CRITICAL (address first)

| Module | Reason |
|---|---|
| `providers.py` | Every API call flows through here. Zero tests. Complex retry logic, provider-specific branching, error classification. A bug here causes silent data corruption or financial waste. |
| `orchestrator/_common.py` | Core adversarial pipeline. Complex async orchestration with multiple failure modes, cost guardrails, catastrophic parse detection. |

### HIGH (address soon)

| Module | Reason |
|---|---|
| `output.py` | User-facing report generation. Formatting errors directly impact usability. |
| `cli.py` | Primary user interface. Signal handling, error propagation, mode routing all untested. |
| `dedup.py` | Deduplication correctness directly affects review quality. Context overflow fallback untested. |
| `orchestrator/_formatting.py` | LLM prompt construction. Incorrect formatting degrades LLM output quality. |
| `orchestrator/code.py` | Full orchestrator mode with zero coverage. |
| `orchestrator/integration.py` | Full orchestrator mode with zero coverage. |
| `orchestrator/spec.py` | Full orchestrator mode with unique non-adversarial pipeline. |

### MEDIUM (fill gaps)

| Module | Reason |
|---|---|
| `parser.py` (gaps) | Spec-mode parsers and normalization helpers untested. |
| `prompts.py` (gaps) | 11 of 13 builder functions untested. |
| `orchestrator/_display.py` | Cost estimation logic worth testing; display functions lower priority. |
| `orchestrator/plan.py` (gaps) | Partial failure and multi-file reference paths untested. |
| `gui/api.py` (gaps) | Success paths for mutations untested. |
| `gui/runner.py` (gaps) | Core runner logic untested. |

### LOW (nice to have)

| Module | Reason |
|---|---|
| `types.py` (gaps) | CostTracker flag behavior, log_fn callback. |
| `revision.py` (gaps) | Spec revision and edge cases. |
| `ui.py` | Trivial module. |
| `gui/pages.py` (gaps) | Data-present page rendering. |
| `gui/_helpers.py` | Trivial. |

---

## Recommended Testing Strategy

### Phase 1: Provider Layer (providers.py)
Use `respx` to mock httpx responses. Test each provider function independently with known request/response pairs. Test the retry engine with simulated failures (429, 529, 5xx, timeout). This is the highest-impact work because it protects against financial waste and silent failures.

### Phase 2: Formatting and Output (output.py, _formatting.py)
These are pure functions with no I/O dependencies. Construct ReviewResult/ReviewGroup objects using the existing conftest.py factories and verify output correctness. High test density for low effort.

### Phase 3: Pipeline Components (_common.py, dedup.py)
Test the individual pipeline functions in isolation first (`_promote_points_to_groups`, `_apply_governance_or_escalate`, `_check_cost_guardrail`), then build async integration tests for `_call_reviewer` and `_run_round2_exchange` with respx mocking.

### Phase 4: Orchestrator Modes (code.py, integration.py, spec.py)
Follow the existing test_orchestrator.py pattern for plan mode. Each mode needs at minimum: happy path, dry run, context exceeded, and cost limit tests.

### Phase 5: CLI (cli.py)
Use Click's `CliRunner` with mocked orchestrator calls. Focus on error propagation, signal handling, and mode routing.

### Phase 6: Remaining Gaps
Fill partial coverage gaps in parser.py, prompts.py, gui modules, and types.py.

---

## Existing Test Infrastructure Notes

The project has solid test infrastructure in place:
- `conftest.py` provides factory helpers: `make_review_point()`, `make_review_group()`, `make_author_response()`, `make_rebuttal()`, `make_author_final()`, `make_model_config()`
- `respx` is already used for HTTP mocking in test_normalization.py and test_orchestrator.py
- `pytest-asyncio` is configured for async test support
- Test naming conventions are consistent and descriptive
- The existing tests demonstrate good patterns that can be extended

The main gap is not infrastructure -- it is simply that the async orchestration layer and its dependencies were never directly tested.
