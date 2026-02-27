# Functional Specification: Role Assignment & Chain-of-Thought Iconography

## Role Assignment Icons (pen-tool, scan-eye, combine, scale, file-pen, puzzle)

- **Behavior:** Bidirectional state machine between Models table and Role Assignments table. Click toggles membership in `{roles}` namespace. DOM syncs horizontally — assignment in either card immediately reflects in the other.
- **Cardinality:** Radio-select for singular roles (author, dedup, normalization, revision, integration); checkbox with ceiling=2 for reviewer.
- **Visual State:** Binary. `role-active` class (illuminated) = assigned; absence = unassigned.
- **Mutation:** Updates `config.roles` in memory; persists via `/api/config` POST on explicit save.

## Chain-of-Thought Icons (brain)

- **Models Table:** Toggle with eligibility gating — click handler active only when model has role assignment (`thinking-eligible`). Mutates `model.thinking` boolean via `/api/config/model-thinking`. Visual state reflects `thinking` property from YAML.
- **Role Assignments Table:** Read-only. Pure function of `modelThinking[assignedModelName]` — no event listeners.
- **State Dependency:** Row existence derives from role assignment; visual state derives independently from `thinking` boolean on the assigned model.

## Critical Constraints

1. **Idempotent init:** `initRolePills()` and `initThinkingToggle()` guard double-attachment via `_rolePillsInitialized` / `_thinkingToggleInitialized` flags.
2. **Hydration order:** Client-side CoT reconciliation deferred until `modelThinking` context is defined (populated by inline script injection post-`DOMContentLoaded`).
3. **Soft navigation:** Logo click → config page must re-evaluate icon states against current `roles` and `modelThinking` without assuming empty initial state.
