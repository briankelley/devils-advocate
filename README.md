# Devil's Advocate

Cost-aware multi-LLM adversarial review engine.

## Install

```
pip install devils-advocate
```

This installs everything including the web dashboard. For development:

```
pip install -e ".[dev]"
```

### Systemd Service (Linux)

To run the GUI as a background service:

```
dvad install
```

This creates and enables a systemd user service on port 8411. See `dvad install --help` for options.

## Quick Start

Review a plan document:

```
dvad review --mode plan --input plan.md --project myproject
```

Review source code against a spec:

```
dvad review --mode code --input src/app.py --spec spec.md --project myproject
```

Run an integration review across project files:

```
dvad review --mode integration --project myproject --project-dir ./
```

## How It Works

Devil's Advocate runs a structured 2-round adversarial protocol:

**Round 1 -- Independent Review + Author Response**

1. Multiple independent reviewer models analyze the input in parallel. Each produces a list of findings (review points) with severity, category, location, and recommendation.
2. A deduplication model groups overlapping findings from different reviewers into consolidated review groups, preserving the original source reviewers.
3. The author model responds to each group with a resolution -- **ACCEPTED**, **REJECTED**, or **PARTIAL** -- along with a rationale and optional revised output.

**Round 2 -- Rebuttal + Final Position**

4. Reviewer models issue rebuttals on **contested groups only** (groups where the author rejected or partially accepted). Each reviewer votes **CONCUR** (agrees with the author) or **CHALLENGE** (disputes the author's position).
5. For challenged groups, the author provides a final response with resolution -- **ACCEPTED**, **REJECTED**, or **MAINTAINED** -- and a final rationale.

**Governance**

6. A deterministic governance engine evaluates the author's final position (Round 2) where available, falling back to the Round 1 position for unchallenged groups. The engine produces a machine-readable decision for each group: `AUTO_ACCEPTED`, `AUTO_DISMISSED`, `ESCALATED`, or `OVERRIDDEN`.

## Governance Decision Rules

The governance engine is deterministic -- no LLM calls, no probability, no judgment. Every group maps to exactly one outcome based on the following rules:

| Scenario | Outcome |
|----------|---------|
| No author response in either round | ESCALATED |
| ACCEPTED, substantive rationale, unchallenged | AUTO_ACCEPTED |
| ACCEPTED, rote/empty rationale | ESCALATED |
| ACCEPTED, challenged, no author final response | ESCALATED |
| ACCEPTED, challenged, substantive final response | AUTO_ACCEPTED |
| PARTIAL (any mode) | ESCALATED |
| REJECTED/MAINTAINED, 2+ reviewers, valid 3-criteria rationale | ESCALATED |
| REJECTED/MAINTAINED, 2+ reviewers, invalid rationale | AUTO_ACCEPTED |
| Single reviewer REJECTED, integration mode | ESCALATED |
| Single reviewer REJECTED, plan/code, unchallenged | AUTO_DISMISSED |
| Single reviewer REJECTED, plan/code, challenged | ESCALATED |
| Unknown resolution | ESCALATED |

**Core invariant:** No finding passes through governance without the author demonstrating engagement. Implicit acceptance and rote acceptance both escalate to human review.

## Configuration Reference

Devil's Advocate is configured via a `models.yaml` file. See `examples/models.yaml.example` for a complete annotated template.

### Models Block

Each entry under `models:` defines a model with the following fields:

| Field | Required | Description |
|-------|----------|-------------|
| `provider` | yes | `anthropic` or `openai` (OpenAI-compatible APIs) |
| `model_id` | yes | The model identifier sent to the provider API |
| `api_key_env` | yes | Environment variable name containing the API key |
| `api_base` | openai only | Base URL for OpenAI-compatible endpoints |
| `context_window` | no | Maximum context window in tokens |
| `cost_per_1k_input` | no | Cost per 1,000 input tokens (USD) |
| `cost_per_1k_output` | no | Cost per 1,000 output tokens (USD) |
| `timeout` | no | Request timeout in seconds (default: 120) |
| `use_completion_tokens` | no | Use `max_completion_tokens` instead of `max_tokens` (for reasoning models) |

### Roles Block

The `roles:` block assigns models to their functions in the review protocol:

| Role | Required | Description |
|------|----------|-------------|
| `author` | yes | Model that responds to findings and produces revisions |
| `reviewers` | yes | List of models that independently analyze the input (minimum 2) |
| `deduplication` | yes | Model that groups overlapping findings (must not be the author) |
| `integration_reviewer` | yes | Reviewer used for integration mode |
| `normalization` | no | Model for severity/category normalization (defaults to dedup model) |

**API keys are resolved from environment variables only -- never put secrets in the config file.**

## Config and Data Paths

### Config Resolution Order

1. `--config` CLI flag (explicit path)
2. `./models.yaml` (project-local)
3. `$DVAD_HOME/models.yaml` (environment variable override)
4. `~/.config/devils-advocate/models.yaml` (XDG default)

### Data Directory

- `$DVAD_HOME` if set
- `~/.local/share/devils-advocate/` otherwise

Review artifacts, ledgers, and logs are stored under the data directory.

### Lock Directory

- `.dvad/` in the project directory (current working directory by default, or `--project-dir`)

Contains the process lock file and optional `manifest.json` for integration mode.

## CLI Reference

### `dvad review`

Run an adversarial review.

```
dvad review --mode <plan|code|integration> [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--mode` | Review mode: `plan`, `code`, or `integration` (required) |
| `--input` | Input file(s) to review (required for plan/code; repeatable) |
| `--spec` | Specification file for code or integration review |
| `--project` | Project name/identifier (required) |
| `--max-cost` | Maximum cost in USD -- abort if exceeded |
| `--dry-run` | Show planned API calls without executing |
| `--config` | Path to models.yaml |
| `--project-dir` | Project directory for integration spec discovery |

### `dvad history`

Show review history for a project.

```
dvad history --project <name> [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--project` | Project name (required) |
| `--review-id` | Show details for a specific review |
| `--config` | Path to models.yaml |
| `--project-dir` | Project directory to search for reviews |

### `dvad config`

Show, validate, or initialize configuration.

```
dvad config [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--show` | Display current configuration and validate |
| `--init` | Create example config at `~/.config/devils-advocate/models.yaml` |
| `--config` | Path to models.yaml |

### `dvad override`

Resolve an escalated governance decision.

```
dvad override --project <name> --review <id> --point <id> --resolution <uphold|dismiss|escalate> [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--project` | Project name (required) |
| `--review` | Review ID (required) |
| `--point` | Point or group ID to override (required) |
| `--resolution` | `uphold` (reviewer was right), `dismiss` (author was right), or `escalate` (keep flagged) (required) |
| `--config` | Path to models.yaml |
| `--project-dir` | Project directory containing `.dvad/` |

## Integration Spec Discovery

In integration mode, Devil's Advocate looks for a project specification in this order:

1. **`--spec` flag** -- explicit path to a spec file, used as-is.
2. **Conventional filenames in `--project-dir`:**
   - `000-strategic-summary.md`
   - `strategic-summary.md`
3. **Manifest-based fallback** -- if no spec is found via the above and a `.dvad/manifest.json` exists, the engine checks `--project-dir` again for `000-strategic-summary.md`.

Input files for integration review can be provided via `--input` or discovered automatically from `.dvad/manifest.json` (completed tasks).

## Cost Tracking

Every LLM call is tracked with per-model token counts and USD cost. Cost information appears in the review report with a per-model breakdown.

**Cost guardrails:**

- At **80% of `--max-cost`**, a warning is emitted.
- At **100% of `--max-cost`**, the review is aborted with a `CostLimitError`.
- Models without `cost_per_1k_input` / `cost_per_1k_output` set are tracked at $0.00 -- cost guardrails are effectively skipped for those models.

## Provider Design Note

Devil's Advocate uses `httpx` directly rather than vendor SDKs. This is intentional -- vendor SDK version churn is a maintenance liability. Direct HTTP calls give full control over request shape and retry behavior.

## License

MIT
