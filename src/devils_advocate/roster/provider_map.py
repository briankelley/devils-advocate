"""Transport conventions per upstream provider — the dvad-owned half of L2.

models.dev cannot supply these. It has no base URL at all for Anthropic,
OpenAI, Google or xAI, and the key names it publishes are frequently not the
ones an operator actually uses (it lists Z.AI's as ``ZHIPU_API_KEY``).

So conventions are *learned from the operator's own config first* — whatever
they already use for a provider is what a new model from that provider gets —
and fall back to a static hint table only for a provider not yet represented.
That way an operator who renamed a key, pinned a regional endpoint, or turned
on streaming for a vendor keeps those choices automatically.
"""

from __future__ import annotations

from collections import Counter

# Fields the scanner is allowed to set on a brand-new entry. Anything absent
# here is either upstream-owned (see gate.py) or operator-owned (never written).
TRANSPORT_FIELDS = (
    "provider",
    "api_base",
    "api_key_env",
    "use_completion_tokens",
    "stream",
)

# Fallback only. Used when the operator has no model from this provider yet.
STATIC_HINTS: dict[str, dict] = {
    "anthropic": {"provider": "anthropic", "api_key_env": "ANTHROPIC_API_KEY"},
    "openai": {
        "provider": "openai",
        "api_key_env": "OPENAI_API_KEY",
        "api_base": "https://api.openai.com/v1",
        "use_completion_tokens": True,
    },
    "google": {
        "provider": "openai",
        "api_key_env": "GOOGLE_API_KEY",
        "api_base": "https://generativelanguage.googleapis.com/v1beta/openai",
    },
    "xai": {
        "provider": "openai",
        "api_key_env": "XAI_API_KEY",
        "api_base": "https://api.x.ai/v1",
    },
    "zai": {
        "provider": "openai",
        "api_key_env": "ZAI_API_KEY",
        "api_base": "https://api.z.ai/api/paas/v4",
        # z.ai buffers non-streaming responses and truncates long generations.
        "stream": True,
    },
    "deepseek": {
        "provider": "openai",
        "api_key_env": "DEEPSEEK_API_KEY",
        "api_base": "https://api.deepseek.com",
    },
    "moonshotai": {
        "provider": "openai",
        "api_key_env": "MOONSHOT_API_KEY",
        "api_base": "https://api.moonshot.ai/v1",
    },
    "minimax": {
        "provider": "minimax",
        "api_key_env": "MINIMAX_API_KEY",
        "api_base": "https://api.minimax.io",
    },
}

# Providers dvad knows how to talk to. A models.dev provider outside this set
# is never proposed, however good its models look.
SUPPORTED_PROVIDERS = tuple(STATIC_HINTS)

# Subscription lanes. Their entries are shaped by the probe, not by this map,
# and their cost fields stay at zero (see apply.py).
CLI_LANES = ("claude-cli", "codex-cli")


def upstream_index(payload: dict) -> dict[str, str]:
    """Map ``model_id`` -> upstream provider id, preferring first-party."""
    index: dict[str, str] = {}
    for provider_id in SUPPORTED_PROVIDERS:
        for model_id in ((payload.get(provider_id) or {}).get("models") or {}):
            index.setdefault(model_id, provider_id)
    return index


def learn_conventions(payload: dict, raw_models: dict) -> dict[str, dict]:
    """Infer each provider's transport fields from the operator's own config.

    *raw_models* is the ``models:`` mapping straight out of models.yaml.
    Subscription-lane entries are skipped — their transport is the CLI, not an
    endpoint, so they say nothing about how to reach the provider's API.

    Where the operator has been inconsistent, the most common value wins. Where
    they have no model from a provider at all, the static hint is used.
    """
    index = upstream_index(payload)
    votes: dict[str, dict[str, Counter]] = {}

    for entry in raw_models.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("provider") in CLI_LANES:
            continue
        provider_id = index.get(entry.get("model_id"))
        if provider_id is None:
            continue
        field_votes = votes.setdefault(provider_id, {})
        for field in TRANSPORT_FIELDS:
            if field in entry:
                value = entry[field]
                # Unhashable values cannot be voted on; skip rather than crash.
                if isinstance(value, (str, bool, int, float, type(None))):
                    field_votes.setdefault(field, Counter())[value] += 1

    conventions: dict[str, dict] = {}
    for provider_id in SUPPORTED_PROVIDERS:
        learned = {
            field: counter.most_common(1)[0][0]
            for field, counter in votes.get(provider_id, {}).items()
            if counter
        }
        # An empty api_key_env is a subscription artefact, not a convention.
        if learned.get("api_key_env") == "":
            learned.pop("api_key_env")
        conventions[provider_id] = {**STATIC_HINTS[provider_id], **learned}
    return conventions
