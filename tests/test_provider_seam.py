"""Tests for the provider seam (Journey 1 / design D1).

Covers the provider registry and dispatch semantics, external provider-plugin
loading, the unresolved-provider validation, the per-model ``extra`` passthrough,
the three new ``ModelConfig`` fields, and the ``min_points_hint`` reviewer knob.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import httpx
import pytest
import respx

import devils_advocate.providers as prov
from devils_advocate.config import load_config
from devils_advocate.prompts import (
    MIN_POINTS_HINT_SENTENCE,
    apply_min_points_hint,
)
from devils_advocate.providers import (
    PROVIDER_REGISTRY,
    ProviderPluginAPI,
    call_anthropic,
    call_minimax,
    call_model,
    call_openai_compatible,
    make_plugin_api,
    register_provider,
)
from devils_advocate.types import ConfigError, ModelConfig


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def registry_guard():
    """Snapshot and restore PROVIDER_REGISTRY around a test.

    The registry is module-level and mutated by register_provider / plugin
    loading; without this, registrations leak into sibling tests.
    """
    snapshot = dict(PROVIDER_REGISTRY)
    yield
    PROVIDER_REGISTRY.clear()
    PROVIDER_REGISTRY.update(snapshot)


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "fake-key-for-testing")


def _make_model(**kwargs) -> ModelConfig:
    kwargs.setdefault("name", "m")
    kwargs.setdefault("provider", "anthropic")
    kwargs.setdefault("model_id", "claude-test")
    kwargs.setdefault("api_key_env", "TEST_KEY")
    return ModelConfig(**kwargs)


def _write_yaml(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))
    return path


def _openai_response(text="Hello", prompt_tokens=10, completion_tokens=5):
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


# ===========================================================================
# The registry — seeding and registration
# ===========================================================================


class TestRegistry:
    def test_seeded_with_builtins(self):
        assert PROVIDER_REGISTRY["anthropic"] is call_anthropic
        assert PROVIDER_REGISTRY["minimax"] is call_minimax
        assert PROVIDER_REGISTRY["local"] is call_openai_compatible

    def test_register_provider_adds_entry(self, registry_guard):
        async def fn(*a, **k):
            return "x", {"input_tokens": 0, "output_tokens": 0}

        register_provider("some-lane", fn)
        assert PROVIDER_REGISTRY["some-lane"] is fn

    def test_make_plugin_api_surface(self):
        api = make_plugin_api()
        assert isinstance(api, ProviderPluginAPI)
        assert api.register_provider is register_provider
        assert api.call_anthropic is call_anthropic
        assert api.call_openai_compatible is call_openai_compatible
        assert api.call_minimax is call_minimax
        assert api.call_with_retry is prov.call_with_retry
        assert api.ConfigError is ConfigError


# ===========================================================================
# call_model dispatch via the registry
# ===========================================================================


class TestDispatch:
    async def test_registered_provider_routes_by_name(self, registry_guard):
        """A provider registered by name is dispatched to for that provider."""
        calls = []

        async def fn(client, model, system_prompt, user_prompt, max_tokens=16384, mode=""):
            calls.append((system_prompt, user_prompt, max_tokens, mode))
            return "from-plugin", {"input_tokens": 3, "output_tokens": 4}

        register_provider("myprov", fn)
        model = _make_model(provider="myprov")
        async with httpx.AsyncClient() as client:
            text, usage = await call_model(client, model, "sys", "usr", mode="plan")

        assert text == "from-plugin"
        assert usage == {"input_tokens": 3, "output_tokens": 4}
        # cache_prefix is NOT forwarded to non-anthropic registered providers.
        assert calls == [("sys", "usr", 16384, "plan")]

    async def test_unregistered_with_api_base_hits_catch_all(self):
        """An unknown provider carrying api_base falls through to openai-compatible."""
        model = _make_model(
            provider="mystery-vendor",
            model_id="gpt-x",
            api_base="https://api.example.com/v1",
        )
        with respx.mock:
            route = respx.post("https://api.example.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_response("catch-all"))
            )
            async with httpx.AsyncClient() as client:
                text, _ = await call_model(client, model, "sys", "usr")

        assert text == "catch-all"
        assert route.called

    async def test_local_outranks_use_responses_api(self):
        """provider='local' dispatches to openai-compatible even with use_responses_api set.

        Pins the ordering the old if/elif guaranteed: `local` is checked before
        the Responses API fallback, so a local model never hits /responses.
        """
        model = _make_model(
            provider="local",
            model_id="local-model",
            api_base="https://local.test/v1",
            use_responses_api=True,
        )
        with respx.mock:
            chat = respx.post("https://local.test/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_response("local wins"))
            )
            responses = respx.post("https://local.test/v1/responses").mock(
                return_value=httpx.Response(200, json={})
            )
            async with httpx.AsyncClient() as client:
                text, _ = await call_model(client, model, "sys", "usr")

        assert text == "local wins"
        assert chat.called
        assert not responses.called

    async def test_unregistered_with_use_responses_api_hits_responses(self):
        """An unknown provider with use_responses_api (no registry hit) uses /responses."""
        model = _make_model(
            provider="openai",
            model_id="gpt-5.3-codex",
            api_base="https://api.openai.com/v1",
            use_responses_api=True,
        )
        response_body = {
            "output": [
                {"content": [{"type": "output_text", "text": "via responses"}]}
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        with respx.mock:
            responses = respx.post("https://api.openai.com/v1/responses").mock(
                return_value=httpx.Response(200, json=response_body)
            )
            async with httpx.AsyncClient() as client:
                text, _ = await call_model(client, model, "sys", "usr")

        assert text == "via responses"
        assert responses.called

    async def test_cache_prefix_forwarded_only_to_anthropic(self):
        """cache_prefix reaches call_anthropic (produces two content blocks)."""
        from devils_advocate.providers import ANTHROPIC_API_URL

        model = _make_model(provider="anthropic")
        prefix = "SHARED PREFIX. "
        with respx.mock:
            route = respx.post(ANTHROPIC_API_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                )
            )
            async with httpx.AsyncClient() as client:
                await call_model(
                    client, model, "sys", prefix + "tail",
                    cache_prefix=prefix,
                )

        import json

        parsed = json.loads(route.calls.last.request.content)
        content = parsed["messages"][0]["content"]
        assert isinstance(content, list)
        assert content[0]["cache_control"] == {"type": "ephemeral"}


# ===========================================================================
# Provider plugin loading (D1.2)
# ===========================================================================


_GOOD_PLUGIN = '''\
def register(api):
    async def _lane(client, model, system_prompt, user_prompt, max_tokens=16384, mode=""):
        return "plugin-hi", {"input_tokens": 1, "output_tokens": 1}
    api.register_provider("plugin-lane", _lane)
'''


def _plugin_config(plugin_path: Path) -> str:
    return f"""\
        settings:
          provider_plugins:
            - {plugin_path}
        models:
          rev:
            provider: plugin-lane
            model_id: x
            api_key_env: TEST_KEY
        roles:
          reviewers:
            - rev
    """


class TestPluginLoading:
    def test_plugin_registers_provider(self, tmp_path, registry_guard):
        plugin = tmp_path / "myplugin.py"
        plugin.write_text(_GOOD_PLUGIN)
        cfg = _write_yaml(tmp_path / "models.yaml", _plugin_config(plugin))

        load_config(cfg)

        assert "plugin-lane" in PROVIDER_REGISTRY

    def test_relative_plugin_path_resolves_against_config_dir(self, tmp_path, registry_guard):
        (tmp_path / "myplugin.py").write_text(_GOOD_PLUGIN)
        cfg = _write_yaml(
            tmp_path / "models.yaml", _plugin_config(Path("myplugin.py"))
        )

        load_config(cfg)

        assert "plugin-lane" in PROVIDER_REGISTRY

    def test_missing_plugin_file_raises_configerror(self, tmp_path, registry_guard):
        cfg = _write_yaml(
            tmp_path / "models.yaml",
            _plugin_config(tmp_path / "does-not-exist.py"),
        )
        with pytest.raises(ConfigError, match="does-not-exist.py"):
            load_config(cfg)

    def test_plugin_without_register_raises_configerror(self, tmp_path, registry_guard):
        plugin = tmp_path / "noreg.py"
        plugin.write_text("X = 1\n")
        cfg = _write_yaml(tmp_path / "models.yaml", _plugin_config(plugin))
        with pytest.raises(ConfigError, match="register"):
            load_config(cfg)

    def test_plugin_register_raising_is_wrapped(self, tmp_path, registry_guard):
        plugin = tmp_path / "boom.py"
        plugin.write_text("def register(api):\n    raise RuntimeError('kaboom')\n")
        cfg = _write_yaml(tmp_path / "models.yaml", _plugin_config(plugin))
        with pytest.raises(ConfigError, match="boom.py"):
            load_config(cfg)

    def test_plugin_import_error_is_wrapped(self, tmp_path, registry_guard):
        plugin = tmp_path / "syntax.py"
        plugin.write_text("def register(api):\n    this is not valid python\n")
        cfg = _write_yaml(tmp_path / "models.yaml", _plugin_config(plugin))
        with pytest.raises(ConfigError, match="syntax.py"):
            load_config(cfg)


# ===========================================================================
# Unresolved-provider validation (D1.3)
# ===========================================================================


class TestProviderValidation:
    def test_bare_unknown_provider_on_active_model_raises(self, tmp_path):
        cfg = _write_yaml(
            tmp_path / "models.yaml",
            """\
            models:
              rev:
                provider: mystery
                model_id: x
                api_key_env: TEST_KEY
            roles:
              reviewers:
                - rev
            """,
        )
        with pytest.raises(ConfigError, match="provider 'mystery' for model 'rev'"):
            load_config(cfg)

    def test_unknown_provider_with_api_base_is_allowed(self, tmp_path):
        cfg = _write_yaml(
            tmp_path / "models.yaml",
            """\
            models:
              rev:
                provider: mystery
                model_id: x
                api_key_env: TEST_KEY
                api_base: https://api.example.com/v1
            roles:
              reviewers:
                - rev
            """,
        )
        config = load_config(cfg)
        assert "rev" in config["models"]

    def test_unknown_provider_with_responses_api_is_allowed(self, tmp_path):
        cfg = _write_yaml(
            tmp_path / "models.yaml",
            """\
            models:
              rev:
                provider: mystery
                model_id: x
                api_key_env: TEST_KEY
                use_responses_api: true
            roles:
              reviewers:
                - rev
            """,
        )
        config = load_config(cfg)
        assert "rev" in config["models"]

    def test_default_openai_provider_without_api_base_is_exempt(self, tmp_path):
        """The default provider sentinel ('openai') is exempt: bare openai loads.

        This is the long-standing 'transport unspecified' placeholder, deferred
        to call-time; D1.3 targets named lane/plugin providers, not the default.
        """
        cfg = _write_yaml(
            tmp_path / "models.yaml",
            """\
            models:
              rev:
                provider: openai
                model_id: gpt-x
                api_key_env: TEST_KEY
            roles:
              reviewers:
                - rev
            """,
        )
        config = load_config(cfg)
        assert "rev" in config["models"]

    def test_inactive_bare_provider_is_not_validated(self, tmp_path):
        """A bare unknown provider on an unused (role-less) model does not fail load."""
        cfg = _write_yaml(
            tmp_path / "models.yaml",
            """\
            models:
              used:
                provider: anthropic
                model_id: claude-test
                api_key_env: TEST_KEY
              unused:
                provider: mystery
                model_id: y
                api_key_env: TEST_KEY
            roles:
              reviewers:
                - used
            """,
        )
        config = load_config(cfg)
        assert "unused" not in config["models"]
        assert "unused" in config["all_models"]


# ===========================================================================
# extra passthrough (D1.4) and new ModelConfig fields (D1.6)
# ===========================================================================


class TestExtraAndNewFields:
    def test_new_fields_default_inert(self):
        m = _make_model(provider="anthropic")
        assert m.extra == {}
        assert m.failover_model == ""
        assert m.min_points_hint is None

    def test_extra_passthrough_verbatim(self, tmp_path):
        cfg = _write_yaml(
            tmp_path / "models.yaml",
            """\
            models:
              rev:
                provider: anthropic
                model_id: claude-test
                api_key_env: TEST_KEY
                extra:
                  api_twin: gpt-5.5
                  nested:
                    k: 1
            roles:
              reviewers:
                - rev
            """,
        )
        config = load_config(cfg)
        assert config["models"]["rev"].extra == {
            "api_twin": "gpt-5.5",
            "nested": {"k": 1},
        }

    def test_absent_extra_is_empty_dict(self, tmp_path):
        cfg = _write_yaml(
            tmp_path / "models.yaml",
            """\
            models:
              rev:
                provider: anthropic
                model_id: claude-test
                api_key_env: TEST_KEY
            roles:
              reviewers:
                - rev
            """,
        )
        config = load_config(cfg)
        assert config["models"]["rev"].extra == {}

    def test_failover_and_hint_parsed(self, tmp_path):
        cfg = _write_yaml(
            tmp_path / "models.yaml",
            """\
            models:
              rev:
                provider: anthropic
                model_id: claude-test
                api_key_env: TEST_KEY
                failover_model: rev-api
                min_points_hint: 20
            roles:
              reviewers:
                - rev
            """,
        )
        config = load_config(cfg)
        m = config["models"]["rev"]
        assert m.failover_model == "rev-api"
        assert m.min_points_hint == 20


# ===========================================================================
# min_points_hint sentence (D1.5)
# ===========================================================================


class TestMinPointsHint:
    def test_none_returns_prompt_unchanged(self):
        assert apply_min_points_hint("PROMPT", None) == "PROMPT"

    def test_zero_returns_prompt_unchanged(self):
        assert apply_min_points_hint("PROMPT", 0) == "PROMPT"

    def test_appends_pinned_sentence(self):
        out = apply_min_points_hint("PROMPT", 20)
        expected = (
            "PROMPT\n\nCompleteness requirement: do not cut your candidate "
            "pass short — if the artifact supports more than 20 candidate "
            "findings, surface at least 20 candidates before selecting the "
            "final reported set. The reporting cap is unchanged."
        )
        assert out == expected

    def test_sentence_appended_after_original(self):
        out = apply_min_points_hint("HEAD", 5)
        assert out.startswith("HEAD\n\n")
        assert out.endswith("The reporting cap is unchanged.")

    def test_number_substituted_both_places(self):
        out = apply_min_points_hint("", 7)
        assert out.count("7") == 2
        assert "{n}" not in out
        assert MIN_POINTS_HINT_SENTENCE  # constant is present/importable
