
<div align="center"><img width="200" alt="dvad-color-white" src="https://github.com/user-attachments/assets/35b89380-cddd-4b70-88ac-e71cb1d867a6" /></div>

# Devil's Advocate

Cost-aware multi-LLM adversarial review engine with deterministic governance.

**Do you have an implementation plan, codebase, or spec created by Claude, GPT, Gemini, Grok, etc and you want the flagship model from competing frontier providers to rip it apart, exposing the holes in logic and potential coding landmines, before a single line of code gets written?**

Devil's Advocate pits multiple LLM reviewers against an LLM author in a structured 2-round adversarial protocol. A deterministic governance engine (straight python, no LLM calls, no probability) resolves every finding into a machine-readable outcome. The result is a vetted artifact where every finding has been accepted, defended, challenged, or escalated (to you for final decision) with full traceability from the first objection through final resolution.

<img width="1358" height="989" alt="dvad main" src="https://github.com/user-attachments/assets/686fa07d-df88-436e-81a9-b4782d722107" />

<img width="1319" height="1000" alt="dvad config" src="https://github.com/user-attachments/assets/e6c0fad0-4181-4885-b064-dadb35a81a81" />

## Requirements

- Python 3.12+
- Bare Minimum: 1 API key from 1 provider, 3 models - an author and two reviewers. (*The other roles: dedup, integration, normalization, revision, can use the same models the author or reviewers use*).
- Comfortably Confrontational: 3 API keys from 3 different providers - one for the author, one per reviewer. (*Different providers mean different blind spots and that's where the friction comes from. Friction is good.*)
- Supported providers: Every frontier provider that uses OpenAI-compatible, Anthropic, or Minimax prompt formatting. (Google Gemini, ChatGPT, Claude, DeepSeek, xAI/Grok, Minimax, Kimi, etc.) 

## Quick Install & Update

```bash
curl -fsSL https://raw.githubusercontent.com/briankelley/devils-advocate/main/install.sh | bash
```

Installs dvad, initializes config, sets up the systemd service, and launches the web GUI on port 8411.

## Manual Install

```bash
python3 -m venv ~/.local/share/devils-advocate/venv
~/.local/share/devils-advocate/venv/bin/pip install devils-advocate
ln -sf ~/.local/share/devils-advocate/venv/bin/dvad ~/.local/bin/dvad
dvad install
```

## Getting Started

### 1. Configure your models

Run `dvad config --init` to generate `~/.config/devils-advocate/models.yaml`, then edit it. Each model needs a provider, model ID, and an environment variable name for its API key. See `examples/models.yaml.example` for a fully annotated template.

### 2. Set your API keys

API keys are resolved from environment variables — never stored in the config file. Set them in your shell or in `~/.config/devils-advocate/.env` (auto-loaded, won't override existing env vars).

### 3. Validate

```bash
dvad config --show
```

## Web GUI

The primary way to use Devil's Advocate. Covers the full workflow — submitting reviews, monitoring progress in real time, resolving escalated findings, and generating revised artifacts.

```bash
dvad gui
```

Opens at `http://127.0.0.1:8411`. The GUI includes a dashboard for submitting and browsing reviews, real-time review progress via SSE with per-model cost tracking, governance override controls, revision generation, and visual model/role configuration with a raw YAML editor.

By default the GUI refuses to bind to non-localhost interfaces. `--allow-nonlocal` overrides this and requires a CSRF token header on all mutating requests.

## How It Works

1. **Independent review** — Multiple reviewer models analyze the input in parallel, producing findings with severity, category, location, and recommendation.
2. **Deduplication** — A dedup model groups overlapping findings into consolidated review groups, preserving source attribution.
3. **Author response** — The author model responds to each group: **ACCEPTED**, **REJECTED**, or **PARTIAL** with a rationale.
4. **Rebuttal** — Reviewers issue rebuttals on contested groups only. Each votes **CONCUR** or **CHALLENGE**.
5. **Final position** — For challenged groups, the author provides a final position.
6. **Governance** — A deterministic engine (no LLM calls, pure rule-based logic) maps every group to an outcome: **AUTO_ACCEPTED**, **AUTO_DISMISSED**, or **ESCALATED**. No finding passes through without the author demonstrating engagement — implicit and rote acceptance both escalate to human review.

Escalated findings are resolved through the GUI's override controls or `dvad override`. After governance, `dvad revise` generates the final revised artifact.

## Review Modes

| Mode          | Protocol      | Input                                               | Output                        |
| ------------- | ------------- | --------------------------------------------------- | ----------------------------- |
| `plan`        | Adversarial   | Plan file + optional reference files                | `revised-plan.md`             |
| `code`        | Adversarial   | Exactly one code file, optional spec                | `revised-diff.patch`          |
| `spec`        | Collaborative | Spec file(s)                                        | `revised-spec-suggestions.md` |
| `integration` | Adversarial   | Input files or `.dvad/manifest.json`, optional spec | `remediation-plan.md`         |

**spec** is non-adversarial — no author, no rebuttals, no governance. Findings are grouped by theme and compiled into a suggestion report. All other modes use the full 2-round adversarial protocol.

## CLI Quick Start

```bash
dvad review --mode plan --input plan.md --input ref.py --project myproject
dvad review --mode code --input src/app.py --spec spec.md --project myproject
dvad review --mode plan --input plan.md --project myproject --max-cost 0.50
dvad review --mode plan --input plan.md --project myproject --dry-run
```

## Design Notes

- **No vendor SDKs.** All provider calls use `httpx` directly — full control over request shape and retry behavior.
- **Deterministic governance.** Zero LLM calls. Every outcome is reproducible from the same inputs.
- **Atomic operations.** File writes use `mkstemp` + `os.replace`. Locking uses `O_CREAT | O_EXCL`.
- **XDG-compliant.** Config and data paths follow the XDG Base Directory specification.

Full CLI reference, configuration schema, governance rules, and cost tracking details are available in the [documentation](docs/).

## License

MIT
