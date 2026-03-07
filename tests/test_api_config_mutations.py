"""Tests for API config mutation endpoints and filesystem browser.

Covers _mutate_yaml_config, /config/model-timeout, /config/model-thinking,
/config/model-max-tokens, /config/settings-toggle, /config/validate,
/config (save), /config/readiness, /config/env endpoints, /review/{id}/override,
/review/{id}/log, /review/{id}/report, /review/{id}/revised, /fs/ls,
and the start_review validation paths.
"""

from __future__ import annotations

import json
import os
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers import make_model_config


# ─── helpers ──────────────────────────────────────────────────────────────

def _make_request(csrf_token="tok-123", config_path=None, json_body=None,
                  headers=None):
    """Build a mock FastAPI Request with app.state attached."""
    request = MagicMock()
    request.app.state.csrf_token = csrf_token
    request.app.state.config_path = config_path

    hdr = {"X-DVAD-Token": csrf_token}
    if headers:
        hdr.update(headers)
    request.headers.get = lambda key, default="": hdr.get(key, default)

    if json_body is not None:
        request.json = AsyncMock(return_value=json_body)

    return request


def _write_yaml(path: Path, content: str) -> None:
    path.write_text(content)


# ═══════════════════════════════════════════════════════════════════════════
# 1. _mutate_yaml_config
# ═══════════════════════════════════════════════════════════════════════════


class TestMutateYamlConfig:
    @pytest.mark.asyncio
    async def test_applies_mutator_and_writes(self, tmp_path):
        from devils_advocate.gui.api import _mutate_yaml_config

        cfg_path = tmp_path / "models.yaml"
        cfg_path.write_text("models:\n  test-model:\n    timeout: 60\n")

        request = _make_request(config_path=str(cfg_path))

        def mutator(data):
            data["models"]["test-model"]["timeout"] = 120

        await _mutate_yaml_config(request, mutator)

        result = cfg_path.read_text()
        assert "120" in result

    @pytest.mark.asyncio
    async def test_creates_backup(self, tmp_path):
        from devils_advocate.gui.api import _mutate_yaml_config

        cfg_path = tmp_path / "models.yaml"
        cfg_path.write_text("models:\n  m:\n    timeout: 30\n")

        request = _make_request(config_path=str(cfg_path))
        await _mutate_yaml_config(request, lambda data: None)

        backup = cfg_path.with_suffix(".yaml.bak")
        assert backup.exists()

    @pytest.mark.asyncio
    async def test_mutator_raising_http_exception_propagates(self, tmp_path):
        from fastapi import HTTPException
        from devils_advocate.gui.api import _mutate_yaml_config

        cfg_path = tmp_path / "models.yaml"
        cfg_path.write_text("models: {}\n")

        request = _make_request(config_path=str(cfg_path))

        def mutator(data):
            raise HTTPException(status_code=404, detail="Not found")

        with pytest.raises(HTTPException) as exc_info:
            await _mutate_yaml_config(request, mutator)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_no_config_path_uses_find_config(self, tmp_path):
        from devils_advocate.gui.api import _mutate_yaml_config

        cfg_path = tmp_path / "models.yaml"
        cfg_path.write_text("models:\n  m:\n    timeout: 30\n")

        request = _make_request(config_path=None)

        with patch("devils_advocate.config.find_config", return_value=cfg_path):
            await _mutate_yaml_config(request, lambda data: None)
        assert cfg_path.exists()

    @pytest.mark.asyncio
    async def test_no_config_path_find_fails_raises_400(self):
        from fastapi import HTTPException
        from devils_advocate.gui.api import _mutate_yaml_config

        request = _make_request(config_path=None)

        with patch("devils_advocate.config.find_config", side_effect=Exception("not found")):
            with pytest.raises(HTTPException) as exc_info:
                await _mutate_yaml_config(request, lambda data: None)
            assert exc_info.value.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# 2. /config/model-timeout
# ═══════════════════════════════════════════════════════════════════════════


class TestModelTimeout:
    @pytest.mark.asyncio
    async def test_valid_timeout_update(self, tmp_path):
        from devils_advocate.gui.api import set_model_timeout

        cfg_path = tmp_path / "models.yaml"
        cfg_path.write_text("models:\n  my-model:\n    timeout: 60\n")

        request = _make_request(
            config_path=str(cfg_path),
            json_body={"model_name": "my-model", "timeout": 300},
        )

        response = await set_model_timeout(request)
        body = json.loads(response.body)
        assert body["status"] == "ok"
        assert body["timeout"] == 300

    @pytest.mark.asyncio
    async def test_missing_model_name(self, tmp_path):
        from fastapi import HTTPException
        from devils_advocate.gui.api import set_model_timeout

        request = _make_request(json_body={"model_name": "", "timeout": 60})
        with pytest.raises(HTTPException) as exc_info:
            await set_model_timeout(request)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_timeout_too_low(self, tmp_path):
        from fastapi import HTTPException
        from devils_advocate.gui.api import set_model_timeout

        request = _make_request(json_body={"model_name": "m", "timeout": 5})
        with pytest.raises(HTTPException) as exc_info:
            await set_model_timeout(request)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_timeout_too_high(self, tmp_path):
        from fastapi import HTTPException
        from devils_advocate.gui.api import set_model_timeout

        request = _make_request(json_body={"model_name": "m", "timeout": 9999})
        with pytest.raises(HTTPException) as exc_info:
            await set_model_timeout(request)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_timeout_non_integer(self, tmp_path):
        from fastapi import HTTPException
        from devils_advocate.gui.api import set_model_timeout

        request = _make_request(json_body={"model_name": "m", "timeout": "fast"})
        with pytest.raises(HTTPException) as exc_info:
            await set_model_timeout(request)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_model_not_in_config(self, tmp_path):
        from fastapi import HTTPException
        from devils_advocate.gui.api import set_model_timeout

        cfg_path = tmp_path / "models.yaml"
        cfg_path.write_text("models:\n  other:\n    timeout: 60\n")

        request = _make_request(
            config_path=str(cfg_path),
            json_body={"model_name": "missing", "timeout": 60},
        )
        with pytest.raises(HTTPException) as exc_info:
            await set_model_timeout(request)
        assert exc_info.value.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# 3. /config/model-thinking
# ═══════════════════════════════════════════════════════════════════════════


class TestModelThinking:
    @pytest.mark.asyncio
    async def test_enable_thinking(self, tmp_path):
        from devils_advocate.gui.api import set_model_thinking

        cfg_path = tmp_path / "models.yaml"
        cfg_path.write_text("models:\n  my-model:\n    thinking: false\n")

        request = _make_request(
            config_path=str(cfg_path),
            json_body={"model_name": "my-model", "thinking": True},
        )

        response = await set_model_thinking(request)
        body = json.loads(response.body)
        assert body["status"] == "ok"
        assert body["thinking"] is True

    @pytest.mark.asyncio
    async def test_missing_model_name(self):
        from fastapi import HTTPException
        from devils_advocate.gui.api import set_model_thinking

        request = _make_request(json_body={"model_name": "", "thinking": True})
        with pytest.raises(HTTPException) as exc_info:
            await set_model_thinking(request)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_thinking_not_boolean(self):
        from fastapi import HTTPException
        from devils_advocate.gui.api import set_model_thinking

        request = _make_request(json_body={"model_name": "m", "thinking": "yes"})
        with pytest.raises(HTTPException) as exc_info:
            await set_model_thinking(request)
        assert exc_info.value.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# 4. /config/model-max-tokens
# ═══════════════════════════════════════════════════════════════════════════


class TestModelMaxTokens:
    @pytest.mark.asyncio
    async def test_set_max_tokens(self, tmp_path):
        from devils_advocate.gui.api import set_model_max_tokens

        cfg_path = tmp_path / "models.yaml"
        cfg_path.write_text("models:\n  my-model:\n    provider: openai\n")

        request = _make_request(
            config_path=str(cfg_path),
            json_body={"model_name": "my-model", "max_out_configured": 8000},
        )

        response = await set_model_max_tokens(request)
        body = json.loads(response.body)
        assert body["status"] == "ok"
        assert body["max_out_configured"] == 8000

    @pytest.mark.asyncio
    async def test_clear_max_tokens(self, tmp_path):
        from devils_advocate.gui.api import set_model_max_tokens

        cfg_path = tmp_path / "models.yaml"
        cfg_path.write_text("models:\n  my-model:\n    max_out_configured: 4000\n")

        request = _make_request(
            config_path=str(cfg_path),
            json_body={"model_name": "my-model", "max_out_configured": None, "clear": True},
        )

        response = await set_model_max_tokens(request)
        body = json.loads(response.body)
        assert body["status"] == "ok"

    @pytest.mark.asyncio
    async def test_null_without_clear_errors(self):
        from fastapi import HTTPException
        from devils_advocate.gui.api import set_model_max_tokens

        request = _make_request(
            json_body={"model_name": "m", "max_out_configured": None},
        )
        with pytest.raises(HTTPException) as exc_info:
            await set_model_max_tokens(request)
        assert exc_info.value.status_code == 400
        assert "clear=true" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_value_too_low(self):
        from fastapi import HTTPException
        from devils_advocate.gui.api import set_model_max_tokens

        request = _make_request(
            json_body={"model_name": "m", "max_out_configured": 0},
        )
        with pytest.raises(HTTPException) as exc_info:
            await set_model_max_tokens(request)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_value_too_high(self):
        from fastapi import HTTPException
        from devils_advocate.gui.api import set_model_max_tokens

        request = _make_request(
            json_body={"model_name": "m", "max_out_configured": 2000000},
        )
        with pytest.raises(HTTPException) as exc_info:
            await set_model_max_tokens(request)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_boolean_value_rejected(self):
        from fastapi import HTTPException
        from devils_advocate.gui.api import set_model_max_tokens

        request = _make_request(
            json_body={"model_name": "m", "max_out_configured": True},
        )
        with pytest.raises(HTTPException) as exc_info:
            await set_model_max_tokens(request)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_exceeds_max_out_stated(self, tmp_path):
        from fastapi import HTTPException
        from devils_advocate.gui.api import set_model_max_tokens

        cfg_path = tmp_path / "models.yaml"
        cfg_path.write_text("models:\n  my-model:\n    max_out_stated: 4096\n")

        request = _make_request(
            config_path=str(cfg_path),
            json_body={"model_name": "my-model", "max_out_configured": 8000},
        )
        with pytest.raises(HTTPException) as exc_info:
            await set_model_max_tokens(request)
        assert exc_info.value.status_code == 400
        assert "cannot exceed" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_missing_model_name(self):
        from fastapi import HTTPException
        from devils_advocate.gui.api import set_model_max_tokens

        request = _make_request(
            json_body={"model_name": "", "max_out_configured": 4000},
        )
        with pytest.raises(HTTPException) as exc_info:
            await set_model_max_tokens(request)
        assert exc_info.value.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# 5. /config/settings-toggle
# ═══════════════════════════════════════════════════════════════════════════


class TestSettingsToggle:
    @pytest.mark.asyncio
    async def test_toggle_live_testing(self, tmp_path):
        from devils_advocate.gui.api import set_settings_toggle

        cfg_path = tmp_path / "models.yaml"
        cfg_path.write_text("models: {}\nsettings:\n  live_testing: false\n")

        request = _make_request(
            config_path=str(cfg_path),
            json_body={"key": "live_testing", "value": True},
        )

        response = await set_settings_toggle(request)
        body = json.loads(response.body)
        assert body["status"] == "ok"
        assert body["value"] is True

    @pytest.mark.asyncio
    async def test_unknown_key_rejected(self):
        from fastapi import HTTPException
        from devils_advocate.gui.api import set_settings_toggle

        request = _make_request(
            json_body={"key": "unknown_flag", "value": True},
        )
        with pytest.raises(HTTPException) as exc_info:
            await set_settings_toggle(request)
        assert exc_info.value.status_code == 400
        assert "Unknown" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_creates_settings_block_if_missing(self, tmp_path):
        from devils_advocate.gui.api import set_settings_toggle

        cfg_path = tmp_path / "models.yaml"
        cfg_path.write_text("models: {}\n")

        request = _make_request(
            config_path=str(cfg_path),
            json_body={"key": "live_testing", "value": True},
        )

        await set_settings_toggle(request)
        content = cfg_path.read_text()
        assert "live_testing" in content


# ═══════════════════════════════════════════════════════════════════════════
# 6. /config/validate
# ═══════════════════════════════════════════════════════════════════════════


class TestValidateEndpoint:
    @pytest.mark.asyncio
    async def test_valid_yaml(self, tmp_path):
        from devils_advocate.gui.api import validate_config_endpoint

        yaml_content = (
            "models:\n"
            "  test-model:\n"
            "    provider: openai\n"
            "    model_id: gpt-4\n"
            "    api_key_env: TEST_KEY\n"
            "roles:\n"
            "  author: test-model\n"
            "  reviewers:\n"
            "    - test-model\n"
        )

        request = _make_request(json_body={"yaml": yaml_content})

        with patch.dict(os.environ, {"TEST_KEY": "sk-test"}, clear=False):
            response = await validate_config_endpoint(request)
        body = json.loads(response.body)
        # May have warnings but we test that it returns a valid structure
        assert "valid" in body
        assert "issues" in body

    @pytest.mark.asyncio
    async def test_invalid_yaml_syntax(self):
        from devils_advocate.gui.api import validate_config_endpoint

        request = _make_request(json_body={"yaml": "models: [invalid yaml: {"})

        response = await validate_config_endpoint(request)
        body = json.loads(response.body)
        assert body["valid"] is False
        assert any("YAML parse error" in issue[1] for issue in body["issues"])

    @pytest.mark.asyncio
    async def test_missing_models_key(self):
        from devils_advocate.gui.api import validate_config_endpoint

        request = _make_request(json_body={"yaml": "settings: {}"})

        response = await validate_config_endpoint(request)
        body = json.loads(response.body)
        assert body["valid"] is False

    @pytest.mark.asyncio
    async def test_empty_models(self):
        from devils_advocate.gui.api import validate_config_endpoint

        request = _make_request(json_body={"yaml": "models: {}"})

        response = await validate_config_endpoint(request)
        body = json.loads(response.body)
        assert body["valid"] is False


# ═══════════════════════════════════════════════════════════════════════════
# 7. /fs/ls
# ═══════════════════════════════════════════════════════════════════════════


class TestFilesystemBrowser:
    @pytest.mark.asyncio
    async def test_home_directory(self):
        from devils_advocate.gui.api import list_directory
        request = _make_request()
        response = await list_directory(request, dir="~")
        body = json.loads(response.body)
        assert body["current_dir"] == str(Path.home())
        assert isinstance(body["entries"], list)

    @pytest.mark.asyncio
    async def test_tmp_directory(self, tmp_path):
        from devils_advocate.gui.api import list_directory

        # Create some test files and dirs
        (tmp_path / "subdir").mkdir()
        (tmp_path / "file.txt").write_text("hello")
        (tmp_path / ".hidden").write_text("secret")

        request = _make_request()
        response = await list_directory(request, dir=str(tmp_path))
        body = json.loads(response.body)

        names = [e["name"] for e in body["entries"]]
        assert "subdir" in names
        assert "file.txt" in names
        assert ".hidden" not in names  # dotfiles filtered

    @pytest.mark.asyncio
    async def test_directories_first(self, tmp_path):
        from devils_advocate.gui.api import list_directory

        (tmp_path / "z_file.txt").write_text("")
        (tmp_path / "a_dir").mkdir()

        request = _make_request()
        response = await list_directory(request, dir=str(tmp_path))
        body = json.loads(response.body)

        assert body["entries"][0]["name"] == "a_dir"
        assert body["entries"][0]["is_dir"] is True

    @pytest.mark.asyncio
    async def test_nonexistent_directory(self):
        from fastapi import HTTPException
        from devils_advocate.gui.api import list_directory

        request = _make_request()
        with pytest.raises(HTTPException) as exc_info:
            await list_directory(request, dir="/nonexistent/path/xyz")
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_not_a_directory(self, tmp_path):
        from fastapi import HTTPException
        from devils_advocate.gui.api import list_directory

        f = tmp_path / "file.txt"
        f.write_text("hello")

        request = _make_request()
        with pytest.raises(HTTPException) as exc_info:
            await list_directory(request, dir=str(f))
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_parent_dir_populated(self, tmp_path):
        from devils_advocate.gui.api import list_directory

        request = _make_request()
        response = await list_directory(request, dir=str(tmp_path))
        body = json.loads(response.body)

        assert body["parent_dir"] == str(tmp_path.parent)

    @pytest.mark.asyncio
    async def test_root_has_no_parent(self):
        from devils_advocate.gui.api import list_directory

        request = _make_request()
        response = await list_directory(request, dir="/")
        body = json.loads(response.body)

        assert body["parent_dir"] is None

    @pytest.mark.asyncio
    async def test_file_size_included(self, tmp_path):
        from devils_advocate.gui.api import list_directory

        f = tmp_path / "data.bin"
        f.write_bytes(b"x" * 42)

        request = _make_request()
        response = await list_directory(request, dir=str(tmp_path))
        body = json.loads(response.body)

        file_entry = [e for e in body["entries"] if e["name"] == "data.bin"][0]
        assert file_entry["size"] == 42
        assert file_entry["is_dir"] is False

    @pytest.mark.asyncio
    async def test_dir_size_is_null(self, tmp_path):
        from devils_advocate.gui.api import list_directory

        (tmp_path / "subdir").mkdir()

        request = _make_request()
        response = await list_directory(request, dir=str(tmp_path))
        body = json.loads(response.body)

        dir_entry = [e for e in body["entries"] if e["name"] == "subdir"][0]
        assert dir_entry["size"] is None


# ═══════════════════════════════════════════════════════════════════════════
# 8. /review/{id}/override
# ═══════════════════════════════════════════════════════════════════════════


class TestOverrideEndpoint:
    @pytest.mark.asyncio
    async def test_valid_override(self, tmp_path):
        from devils_advocate.gui.api import override_group
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.set_review_id("rev-001")
        ledger = {
            "mode": "plan", "project": "test", "result": "complete",
            "points": [{
                "point_id": "p1", "group_id": "g1", "reviewer": "r1",
                "severity": "high", "category": "security",
                "description": "Issue", "recommendation": "Fix",
                "final_resolution": "escalated",
            }],
        }
        storage.save_review_artifacts("rev-001", "# Report", ledger)

        request = _make_request(json_body={
            "group_id": "g1",
            "resolution": "overridden",
        })

        with patch("devils_advocate.gui.api.get_gui_storage", return_value=storage):
            with patch("devils_advocate.gui.pages._review_cache", {"data": "cached"}):
                response = await override_group(request, "rev-001")
        body = json.loads(response.body)
        assert body["status"] == "ok"
        assert body["resolution"] == "overridden"

    @pytest.mark.asyncio
    async def test_invalid_resolution(self):
        from fastapi import HTTPException
        from devils_advocate.gui.api import override_group

        request = _make_request(json_body={
            "group_id": "g1",
            "resolution": "invalid_value",
        })
        with pytest.raises(HTTPException) as exc_info:
            await override_group(request, "rev-001")
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_missing_group_id(self):
        from fastapi import HTTPException
        from devils_advocate.gui.api import override_group

        request = _make_request(json_body={
            "group_id": "",
            "resolution": "overridden",
        })
        with pytest.raises(HTTPException) as exc_info:
            await override_group(request, "rev-001")
        assert exc_info.value.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# 9. /review/{id}/log
# ═══════════════════════════════════════════════════════════════════════════


class TestLogEndpoint:
    @pytest.mark.asyncio
    async def test_log_found(self, tmp_path):
        from devils_advocate.gui.api import get_review_log
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        log_dir = storage.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "rev-001.log"
        log_file.write_text("Line 1\nLine 2\n")

        request = _make_request()

        with patch("devils_advocate.gui.api.get_gui_storage", return_value=storage):
            response = await get_review_log(request, "rev-001")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_log_not_found(self, tmp_path):
        from fastapi import HTTPException
        from devils_advocate.gui.api import get_review_log
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)

        request = _make_request()

        with patch("devils_advocate.gui.api.get_gui_storage", return_value=storage):
            with pytest.raises(HTTPException) as exc_info:
                await get_review_log(request, "missing")
            assert exc_info.value.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# 10. /review/{id}/report
# ═══════════════════════════════════════════════════════════════════════════


class TestReportEndpoint:
    @pytest.mark.asyncio
    async def test_report_found(self, tmp_path):
        from devils_advocate.gui.api import download_report
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        review_dir = storage.reviews_dir / "rev-001"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / "dvad-report.md").write_text("# Report\n")

        request = _make_request()

        with patch("devils_advocate.gui.api.get_gui_storage", return_value=storage):
            response = await download_report(request, "rev-001")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_report_not_found(self, tmp_path):
        from fastapi import HTTPException
        from devils_advocate.gui.api import download_report
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)

        request = _make_request()

        with patch("devils_advocate.gui.api.get_gui_storage", return_value=storage):
            with pytest.raises(HTTPException) as exc_info:
                await download_report(request, "missing")
            assert exc_info.value.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# 11. /review/{id}/revised download
# ═══════════════════════════════════════════════════════════════════════════


class TestRevisedDownload:
    @pytest.mark.asyncio
    async def test_revised_plan_found(self, tmp_path):
        from devils_advocate.gui.api import download_revised
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        review_dir = storage.reviews_dir / "rev-001"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / "revised-plan.md").write_text("# Revised Plan\n")

        request = _make_request()

        with patch("devils_advocate.gui.api.get_gui_storage", return_value=storage):
            response = await download_revised(request, "rev-001")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_revised_patch_found(self, tmp_path):
        from devils_advocate.gui.api import download_revised
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        review_dir = storage.reviews_dir / "rev-001"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / "revised-diff.patch").write_text("--- a/file\n+++ b/file\n")

        request = _make_request()

        with patch("devils_advocate.gui.api.get_gui_storage", return_value=storage):
            response = await download_revised(request, "rev-001")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_no_revised_artifact(self, tmp_path):
        from fastapi import HTTPException
        from devils_advocate.gui.api import download_revised
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        review_dir = storage.reviews_dir / "rev-001"
        review_dir.mkdir(parents=True, exist_ok=True)

        request = _make_request()

        with patch("devils_advocate.gui.api.get_gui_storage", return_value=storage):
            with pytest.raises(HTTPException) as exc_info:
                await download_revised(request, "rev-001")
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_remediation_plan_found(self, tmp_path):
        from devils_advocate.gui.api import download_revised
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        review_dir = storage.reviews_dir / "rev-001"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / "remediation-plan.md").write_text("# Remediation\n")

        request = _make_request()

        with patch("devils_advocate.gui.api.get_gui_storage", return_value=storage):
            response = await download_revised(request, "rev-001")
        assert response.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# 12. start_review validation
# ═══════════════════════════════════════════════════════════════════════════


class TestStartReviewValidation:
    """Tests for the input validation in start_review (not the full flow)."""

    @pytest.mark.asyncio
    async def test_missing_csrf_raises_403(self):
        from fastapi import HTTPException
        from devils_advocate.gui.api import _check_csrf

        request = _make_request(csrf_token="real-token")
        request.headers.get = lambda key, default="": (
            "wrong-token" if key == "X-DVAD-Token" else default
        )
        with pytest.raises(HTTPException) as exc_info:
            _check_csrf(request)
        assert exc_info.value.status_code == 403

    def test_max_file_size_constant(self):
        from devils_advocate.gui.api import MAX_FILE_SIZE
        assert MAX_FILE_SIZE == 10 * 1024 * 1024

    def test_max_files_constant(self):
        from devils_advocate.gui.api import MAX_FILES
        assert MAX_FILES == 25


# ═══════════════════════════════════════════════════════════════════════════
# 13. /review/{id} (get_review_json)
# ═══════════════════════════════════════════════════════════════════════════


class TestGetReviewJson:
    @pytest.mark.asyncio
    async def test_review_found(self, tmp_path):
        from devils_advocate.gui.api import get_review_json
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.set_review_id("rev-001")
        ledger = {"mode": "plan", "project": "test", "result": "complete", "cost": {"total_usd": 0.5}}
        storage.save_review_artifacts("rev-001", "# Report", ledger)

        request = _make_request()

        with patch("devils_advocate.gui.api.get_gui_storage", return_value=storage):
            response = await get_review_json(request, "rev-001")
        body = json.loads(response.body)
        assert body["mode"] == "plan"

    @pytest.mark.asyncio
    async def test_review_not_found(self, tmp_path):
        from fastapi import HTTPException
        from devils_advocate.gui.api import get_review_json
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)

        request = _make_request()

        with patch("devils_advocate.gui.api.get_gui_storage", return_value=storage):
            with pytest.raises(HTTPException) as exc_info:
                await get_review_json(request, "missing")
            assert exc_info.value.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# 14. cancel_review endpoint
# ═══════════════════════════════════════════════════════════════════════════


class TestCancelReviewEndpoint:
    @pytest.mark.asyncio
    async def test_cancel_running_review(self):
        from devils_advocate.gui.api import cancel_review

        runner = MagicMock()
        runner.cancel_review.return_value = True

        request = _make_request()
        request.app.state.runner = runner

        response = await cancel_review(request, "rev-001")
        body = json.loads(response.body)
        assert body["status"] == "ok"

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_review(self):
        from fastapi import HTTPException
        from devils_advocate.gui.api import cancel_review

        runner = MagicMock()
        runner.cancel_review.return_value = False

        request = _make_request()
        request.app.state.runner = runner

        with pytest.raises(HTTPException) as exc_info:
            await cancel_review(request, "missing")
        assert exc_info.value.status_code == 404
