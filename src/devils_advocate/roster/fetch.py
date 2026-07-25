"""Stage 1 — fetch the upstream catalogue, and refuse to trust a bad one.

The scan runs unattended against a third-party endpoint, so the payload is
guarded before anything downstream sees it. A truncated response, an error page
served with a 200, or an index that has lost half its providers must not be
allowed to look like "the catalogue shrank" — under L1 nothing is ever removed
on the strength of an absence, but a degraded payload could still starve the
candidate list and produce a misleading "nothing new today".
"""

from __future__ import annotations

import hashlib
import json

from .provider_map import SUPPORTED_PROVIDERS
from .types import FetchError

MODELS_DEV_URL = "https://models.dev/api.json"
DEFAULT_TIMEOUT = 60

# Sanity floors. The live payload is ~3.2 MB across 172 providers; these are set
# far below that so ordinary upstream churn never trips them.
MIN_PAYLOAD_BYTES = 250_000
MIN_PROVIDERS = 40
# If these are missing, the document is not the catalogue we think it is.
BEDROCK_PROVIDERS = ("anthropic", "openai")


def sanity_check(payload: dict, raw_bytes: int) -> None:
    """Raise :class:`FetchError` if the payload cannot be trusted."""
    if raw_bytes < MIN_PAYLOAD_BYTES:
        raise FetchError(
            f"payload is {raw_bytes} bytes, below the {MIN_PAYLOAD_BYTES} floor "
            "— refusing to treat a truncated response as the catalogue"
        )
    if not isinstance(payload, dict):
        raise FetchError(f"payload is {type(payload).__name__}, expected an object")
    if len(payload) < MIN_PROVIDERS:
        raise FetchError(
            f"payload lists {len(payload)} providers, below the {MIN_PROVIDERS} floor"
        )
    missing = [p for p in BEDROCK_PROVIDERS if not (payload.get(p) or {}).get("models")]
    if missing:
        raise FetchError(f"payload is missing models for {', '.join(missing)}")
    known = [p for p in SUPPORTED_PROVIDERS if (payload.get(p) or {}).get("models")]
    if len(known) < 3:
        raise FetchError(
            f"payload carries only {len(known)} of the {len(SUPPORTED_PROVIDERS)} "
            "providers dvad supports"
        )


def digest(payload: dict) -> str:
    """Stable content hash. Identical catalogues hash identically across runs."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def fetch(url: str = MODELS_DEV_URL, timeout: int = DEFAULT_TIMEOUT) -> tuple[dict, str]:
    """Fetch and validate the catalogue. Returns ``(payload, sha256)``."""
    import httpx

    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        raw = response.content
    except Exception as exc:
        raise FetchError(f"could not fetch {url}: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FetchError(f"{url} did not return JSON: {exc}") from exc

    sanity_check(payload, len(raw))
    return payload, digest(payload)
