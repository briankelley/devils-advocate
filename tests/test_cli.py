"""Tests for devils_advocate.cli module."""

import json
import socket
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from devils_advocate.cli import cli
from devils_advocate.types import (
    APIError,
    ConfigError,
    CostLimitError,
    Resolution,
    StorageError,
)


# ─── Local Helpers ──────────────────────────────────────────────────────────


def _write_input_file(tmp_path, name="plan.md", content="# Test Plan\nThis is a test."):
    """Write a dummy input file and return its path."""
    f = tmp_path / name
    f.write_text(content)
    return f


@pytest.fixture
def runner():
    """Click CliRunner instance."""
    return CliRunner()


# ─── TestCliGroup ───────────────────────────────────────────────────────────


class TestCliGroup:
    """Tests for the top-level cli() group."""

    def test_help_output(self, runner):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Devil's Advocate" in result.output
        assert "review" in result.output
        assert "history" in result.output
        assert "config" in result.output

    def test_version_output(self, runner):
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "dvad" in result.output

    def test_no_args_shows_usage(self, runner):
        result = runner.invoke(cli, [])
        # Click groups with invoke_without_command not set exit with code 2
        assert "Usage" in result.output


# ─── TestReviewCommand ──────────────────────────────────────────────────────


class TestReviewCommand:
    """Tests for the review command."""

    def test_missing_mode_arg(self, runner):
        result = runner.invoke(cli, ["review", "--project", "test"])
        assert result.exit_code != 0

    def test_missing_project_arg(self, runner):
        result = runner.invoke(cli, ["review", "--mode", "plan"])
        assert result.exit_code != 0

    def test_invalid_mode_value(self, runner):
        result = runner.invoke(cli, [
            "review", "--mode", "bogus", "--project", "test",
        ])
        assert result.exit_code != 0

    def test_config_load_error_exits_1(self, runner, tmp_path):
        """ConfigError during load_config prints error and exits 1."""
        with patch("devils_advocate.cli.load_config", side_effect=ConfigError("bad yaml")):
            result = runner.invoke(cli, [
                "review", "--mode", "plan", "--project", "test",
                "--input", str(_write_input_file(tmp_path)),
            ])
        assert result.exit_code == 1
        assert "Config error" in result.output
        assert "bad yaml" in result.output

    def test_config_validation_errors_exit_1(self, runner, tmp_path):
        """Validation errors cause exit 1 with error messages printed."""
        mock_config = {"models": {}, "config_path": "/fake"}
        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.validate_config", return_value=[
                 ("error", "No reviewers configured"),
                 ("error", "Missing API keys"),
             ]):
            result = runner.invoke(cli, [
                "review", "--mode", "plan", "--project", "test",
                "--input", str(_write_input_file(tmp_path)),
            ])
        assert result.exit_code == 1
        assert "No reviewers configured" in result.output
        assert "Missing API keys" in result.output

    def test_config_validation_warnings_printed(self, runner, tmp_path):
        """Validation warnings are printed but do not cause exit."""
        inp = _write_input_file(tmp_path)
        mock_config = {"models": {}, "config_path": "/fake"}

        async def _noop(*a, **kw):
            return None

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.validate_config", return_value=[
                 ("warn", "Model X has no context_window"),
             ]), \
             patch("devils_advocate.cli.run_plan_review", side_effect=_noop), \
             patch("devils_advocate.cli.StorageManager"):
            result = runner.invoke(cli, [
                "review", "--mode", "plan", "--project", "test",
                "--input", str(inp),
            ])
        assert "Warning" in result.output
        assert "Model X has no context_window" in result.output

    def test_plan_mode_missing_input_exits_1(self, runner):
        """Plan mode without --input exits 1."""
        mock_config = {"models": {}, "config_path": "/fake"}
        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.validate_config", return_value=[]):
            result = runner.invoke(cli, [
                "review", "--mode", "plan", "--project", "test",
            ])
        assert result.exit_code == 1
        assert "--input is required" in result.output

    def test_code_mode_missing_input_exits_1(self, runner):
        """Code mode without --input exits 1."""
        mock_config = {"models": {}, "config_path": "/fake"}
        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.validate_config", return_value=[]):
            result = runner.invoke(cli, [
                "review", "--mode", "code", "--project", "test",
            ])
        assert result.exit_code == 1
        assert "--input is required" in result.output

    def test_spec_mode_missing_input_exits_1(self, runner):
        """Spec mode without --input exits 1."""
        mock_config = {"models": {}, "config_path": "/fake"}
        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.validate_config", return_value=[]):
            result = runner.invoke(cli, [
                "review", "--mode", "spec", "--project", "test",
            ])
        assert result.exit_code == 1
        assert "--input is required" in result.output

    def test_input_file_not_found_exits_1(self, runner, tmp_path):
        """Non-existent input file exits 1."""
        mock_config = {"models": {}, "config_path": "/fake"}
        missing = tmp_path / "nonexistent.md"
        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.validate_config", return_value=[]):
            result = runner.invoke(cli, [
                "review", "--mode", "plan", "--project", "test",
                "--input", str(missing),
            ])
        assert result.exit_code == 1
        assert "Input file not found" in result.output

    def test_plan_mode_routes_to_run_plan_review(self, runner, tmp_path):
        """Plan mode calls run_plan_review with correct args."""
        inp = _write_input_file(tmp_path)
        mock_config = {"models": {}, "config_path": "/fake"}
        captured = {}

        async def fake_plan_review(config, input_files, project, max_cost, dry_run):
            captured["config"] = config
            captured["input_files"] = input_files
            captured["project"] = project
            captured["max_cost"] = max_cost
            captured["dry_run"] = dry_run

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.validate_config", return_value=[]), \
             patch("devils_advocate.cli.run_plan_review", side_effect=fake_plan_review), \
             patch("devils_advocate.cli.StorageManager"):
            result = runner.invoke(cli, [
                "review", "--mode", "plan", "--project", "myproj",
                "--input", str(inp), "--max-cost", "5.0", "--dry-run",
            ])
        assert result.exit_code == 0
        assert captured["project"] == "myproj"
        assert captured["max_cost"] == 5.0
        assert captured["dry_run"] is True
        assert len(captured["input_files"]) == 1

    def test_code_mode_routes_to_run_code_review(self, runner, tmp_path):
        """Code mode calls run_code_review."""
        inp = _write_input_file(tmp_path, name="main.py", content="print('hello')")
        mock_config = {"models": {}, "config_path": "/fake"}
        captured = {}

        async def fake_code_review(config, input_file, project, spec_file, max_cost, dry_run):
            captured["input_file"] = input_file
            captured["project"] = project
            captured["spec_file"] = spec_file

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.validate_config", return_value=[]), \
             patch("devils_advocate.cli.run_code_review", side_effect=fake_code_review), \
             patch("devils_advocate.cli.StorageManager"):
            result = runner.invoke(cli, [
                "review", "--mode", "code", "--project", "test",
                "--input", str(inp),
            ])
        assert result.exit_code == 0
        assert captured["project"] == "test"
        assert captured["input_file"] == inp
        assert captured["spec_file"] is None

    def test_code_mode_with_spec_file(self, runner, tmp_path):
        """Code mode with --spec passes the spec file."""
        inp = _write_input_file(tmp_path, "main.py", "code")
        spec = _write_input_file(tmp_path, "spec.md", "spec content")
        mock_config = {"models": {}, "config_path": "/fake"}
        captured = {}

        async def fake_code_review(config, input_file, project, spec_file, max_cost, dry_run):
            captured["spec_file"] = spec_file

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.validate_config", return_value=[]), \
             patch("devils_advocate.cli.run_code_review", side_effect=fake_code_review), \
             patch("devils_advocate.cli.StorageManager"):
            result = runner.invoke(cli, [
                "review", "--mode", "code", "--project", "test",
                "--input", str(inp), "--spec", str(spec),
            ])
        assert result.exit_code == 0
        assert captured["spec_file"] == spec

    def test_code_mode_spec_file_not_found(self, runner, tmp_path):
        """Code mode with non-existent --spec exits 1."""
        inp = _write_input_file(tmp_path, "main.py", "code")
        mock_config = {"models": {}, "config_path": "/fake"}

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.validate_config", return_value=[]):
            result = runner.invoke(cli, [
                "review", "--mode", "code", "--project", "test",
                "--input", str(inp), "--spec", str(tmp_path / "missing.md"),
            ])
        assert result.exit_code == 1
        assert "Spec file not found" in result.output

    def test_integration_mode_routes_to_run_integration_review(self, runner, tmp_path):
        """Integration mode calls run_integration_review."""
        mock_config = {"models": {}, "config_path": "/fake"}
        captured = {}

        async def fake_integration_review(config, project, **kwargs):
            captured["project"] = project
            captured.update(kwargs)

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.validate_config", return_value=[]), \
             patch("devils_advocate.cli.run_integration_review", side_effect=fake_integration_review), \
             patch("devils_advocate.cli.StorageManager"):
            result = runner.invoke(cli, [
                "review", "--mode", "integration", "--project", "test",
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 0
        assert captured["project"] == "test"
        assert captured["project_dir"] == tmp_path

    def test_spec_mode_routes_to_run_spec_review(self, runner, tmp_path):
        """Spec mode calls run_spec_review."""
        inp = _write_input_file(tmp_path, "spec.md", "spec content")
        mock_config = {"models": {}, "config_path": "/fake"}
        captured = {}

        async def fake_spec_review(config, input_files, project, max_cost, dry_run):
            captured["input_files"] = input_files
            captured["project"] = project

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.validate_config", return_value=[]), \
             patch("devils_advocate.cli.run_spec_review", side_effect=fake_spec_review), \
             patch("devils_advocate.cli.StorageManager"):
            result = runner.invoke(cli, [
                "review", "--mode", "spec", "--project", "test",
                "--input", str(inp),
            ])
        assert result.exit_code == 0
        assert captured["project"] == "test"

    def test_api_error_exits_1(self, runner, tmp_path):
        """APIError during review prints message and exits 1."""
        inp = _write_input_file(tmp_path)
        mock_config = {"models": {}, "config_path": "/fake"}

        async def raise_api_error(*a, **kw):
            raise APIError("Rate limit exceeded")

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.validate_config", return_value=[]), \
             patch("devils_advocate.cli.run_plan_review", side_effect=raise_api_error), \
             patch("devils_advocate.cli.StorageManager"):
            result = runner.invoke(cli, [
                "review", "--mode", "plan", "--project", "test",
                "--input", str(inp),
            ])
        assert result.exit_code == 1
        assert "Aborted" in result.output
        assert "Rate limit exceeded" in result.output

    def test_cost_limit_error_exits_1(self, runner, tmp_path):
        """CostLimitError during review prints message and exits 1."""
        inp = _write_input_file(tmp_path)
        mock_config = {"models": {}, "config_path": "/fake"}

        async def raise_cost_error(*a, **kw):
            raise CostLimitError("Budget of $1.00 exceeded")

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.validate_config", return_value=[]), \
             patch("devils_advocate.cli.run_plan_review", side_effect=raise_cost_error), \
             patch("devils_advocate.cli.StorageManager"):
            result = runner.invoke(cli, [
                "review", "--mode", "plan", "--project", "test",
                "--input", str(inp),
            ])
        assert result.exit_code == 1
        assert "Budget of $1.00 exceeded" in result.output

    def test_keyboard_interrupt_exits_130(self, runner, tmp_path):
        """KeyboardInterrupt during review prints Interrupted and exits 130."""
        inp = _write_input_file(tmp_path)
        mock_config = {"models": {}, "config_path": "/fake"}

        async def raise_keyboard_interrupt(*a, **kw):
            raise KeyboardInterrupt()

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.validate_config", return_value=[]), \
             patch("devils_advocate.cli.run_plan_review", side_effect=raise_keyboard_interrupt), \
             patch("devils_advocate.cli.StorageManager"):
            result = runner.invoke(cli, [
                "review", "--mode", "plan", "--project", "test",
                "--input", str(inp),
            ])
        assert result.exit_code == 130
        assert "Interrupted" in result.output

    def test_dry_run_forwarded(self, runner, tmp_path):
        """--dry-run flag is forwarded to the orchestrator."""
        inp = _write_input_file(tmp_path)
        mock_config = {"models": {}, "config_path": "/fake"}
        captured = {}

        async def capture_plan(config, input_files, project, max_cost, dry_run):
            captured["dry_run"] = dry_run

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.validate_config", return_value=[]), \
             patch("devils_advocate.cli.run_plan_review", side_effect=capture_plan), \
             patch("devils_advocate.cli.StorageManager"):
            result = runner.invoke(cli, [
                "review", "--mode", "plan", "--project", "test",
                "--input", str(inp), "--dry-run",
            ])
        assert result.exit_code == 0
        assert captured["dry_run"] is True

    def test_max_cost_forwarded(self, runner, tmp_path):
        """--max-cost value is forwarded to the orchestrator."""
        inp = _write_input_file(tmp_path)
        mock_config = {"models": {}, "config_path": "/fake"}
        captured = {}

        async def capture_plan(config, input_files, project, max_cost, dry_run):
            captured["max_cost"] = max_cost

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.validate_config", return_value=[]), \
             patch("devils_advocate.cli.run_plan_review", side_effect=capture_plan), \
             patch("devils_advocate.cli.StorageManager"):
            result = runner.invoke(cli, [
                "review", "--mode", "plan", "--project", "test",
                "--input", str(inp), "--max-cost", "2.50",
            ])
        assert result.exit_code == 0
        assert captured["max_cost"] == 2.50

    def test_config_path_forwarded(self, runner, tmp_path):
        """--config path is forwarded to load_config."""
        inp = _write_input_file(tmp_path)
        cfg = tmp_path / "models.yaml"
        cfg.write_text("placeholder")
        captured = {}

        def capture_load(path=None):
            captured["config_path"] = path
            return {"models": {}, "config_path": str(cfg)}

        async def noop(*a, **kw):
            pass

        with patch("devils_advocate.cli.load_config", side_effect=capture_load), \
             patch("devils_advocate.cli.validate_config", return_value=[]), \
             patch("devils_advocate.cli.run_plan_review", side_effect=noop), \
             patch("devils_advocate.cli.StorageManager"):
            result = runner.invoke(cli, [
                "review", "--mode", "plan", "--project", "test",
                "--input", str(inp), "--config", str(cfg),
            ])
        assert result.exit_code == 0
        assert captured["config_path"] == cfg

    def test_multiple_input_files(self, runner, tmp_path):
        """Multiple --input flags are collected into a list."""
        f1 = _write_input_file(tmp_path, "a.md", "file a")
        f2 = _write_input_file(tmp_path, "b.md", "file b")
        mock_config = {"models": {}, "config_path": "/fake"}
        captured = {}

        async def fake_plan(config, input_files, project, max_cost, dry_run):
            captured["input_files"] = input_files

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.validate_config", return_value=[]), \
             patch("devils_advocate.cli.run_plan_review", side_effect=fake_plan), \
             patch("devils_advocate.cli.StorageManager"):
            result = runner.invoke(cli, [
                "review", "--mode", "plan", "--project", "test",
                "--input", str(f1), "--input", str(f2),
            ])
        assert result.exit_code == 0
        assert len(captured["input_files"]) == 2

    def test_integration_mode_does_not_require_input(self, runner, tmp_path):
        """Integration mode does not require --input."""
        mock_config = {"models": {}, "config_path": "/fake"}

        async def fake_integration(config, project, **kwargs):
            pass

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.validate_config", return_value=[]), \
             patch("devils_advocate.cli.run_integration_review", side_effect=fake_integration), \
             patch("devils_advocate.cli.StorageManager"):
            result = runner.invoke(cli, [
                "review", "--mode", "integration", "--project", "test",
            ])
        # Integration mode should not require --input
        assert result.exit_code == 0


# ─── TestHistoryCommand ─────────────────────────────────────────────────────


class TestHistoryCommand:
    """Tests for the history command."""

    def test_missing_project_arg(self, runner):
        result = runner.invoke(cli, ["history"])
        assert result.exit_code != 0

    def test_list_reviews_empty(self, runner, tmp_path):
        """No reviews found prints a friendly message."""
        mock_storage = MagicMock()
        mock_storage.list_reviews.return_value = []

        with patch("devils_advocate.cli.StorageManager", return_value=mock_storage):
            result = runner.invoke(cli, [
                "history", "--project", "test", "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 0
        assert "No reviews found" in result.output

    def test_list_reviews_shows_table(self, runner, tmp_path):
        """Reviews are displayed in a table format."""
        mock_storage = MagicMock()
        mock_storage.list_reviews.return_value = [
            {
                "review_id": "rev-001",
                "mode": "plan",
                "input_file": "plan.md",
                "timestamp": "2026-02-14T18:26:00Z",
                "total_points": 5,
                "total_cost": 0.0342,
            },
        ]

        with patch("devils_advocate.cli.StorageManager", return_value=mock_storage):
            result = runner.invoke(cli, [
                "history", "--project", "test", "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 0
        assert "rev-001" in result.output
        assert "plan" in result.output

    def test_detail_view_renders_report(self, runner, tmp_path):
        """--review-id with an existing report renders Markdown."""
        review_id = "rev-001"
        mock_storage = MagicMock()
        mock_storage.load_review.return_value = {"review_id": review_id, "points": []}
        reviews_dir = tmp_path / ".dvad" / "reviews"
        mock_storage.reviews_dir = reviews_dir
        rd = reviews_dir / review_id
        rd.mkdir(parents=True)
        report = rd / "dvad-report.md"
        report.write_text("# Test Report\nAll findings addressed.")

        with patch("devils_advocate.cli.StorageManager", return_value=mock_storage):
            result = runner.invoke(cli, [
                "history", "--project", "test",
                "--review-id", review_id,
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 0
        assert "Test Report" in result.output

    def test_detail_view_falls_back_to_json(self, runner, tmp_path):
        """--review-id without a report file falls back to JSON dump."""
        review_id = "rev-002"
        mock_storage = MagicMock()
        mock_storage.load_review.return_value = {"review_id": review_id, "mode": "plan"}
        reviews_dir = tmp_path / ".dvad" / "reviews"
        mock_storage.reviews_dir = reviews_dir
        rd = reviews_dir / review_id
        rd.mkdir(parents=True)
        # No dvad-report.md -> fallback to JSON

        with patch("devils_advocate.cli.StorageManager", return_value=mock_storage):
            result = runner.invoke(cli, [
                "history", "--project", "test",
                "--review-id", review_id,
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 0
        assert "rev-002" in result.output

    def test_review_not_found_exits_1(self, runner, tmp_path):
        """--review-id with nonexistent review exits 1."""
        mock_storage = MagicMock()
        mock_storage.load_review.return_value = None

        with patch("devils_advocate.cli.StorageManager", return_value=mock_storage):
            result = runner.invoke(cli, [
                "history", "--project", "test",
                "--review-id", "nonexistent",
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_project_dir_option(self, runner, tmp_path):
        """--project-dir is passed to StorageManager."""
        captured = {}

        def capture_storage(base_dir):
            captured["base_dir"] = base_dir
            m = MagicMock()
            m.list_reviews.return_value = []
            return m

        with patch("devils_advocate.cli.StorageManager", side_effect=capture_storage):
            result = runner.invoke(cli, [
                "history", "--project", "test",
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 0
        assert captured["base_dir"] == tmp_path

    def test_list_reviews_multiple(self, runner, tmp_path):
        """Multiple reviews are all shown in the table."""
        mock_storage = MagicMock()
        mock_storage.list_reviews.return_value = [
            {
                "review_id": f"rev-{i:03d}",
                "mode": "plan",
                "input_file": f"file{i}.md",
                "timestamp": f"2026-02-14T00:00:0{i}Z",
                "total_points": i + 1,
                "total_cost": 0.01 * (i + 1),
            }
            for i in range(3)
        ]

        with patch("devils_advocate.cli.StorageManager", return_value=mock_storage):
            result = runner.invoke(cli, [
                "history", "--project", "test", "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 0
        assert "rev-000" in result.output
        assert "rev-001" in result.output
        assert "rev-002" in result.output


# ─── TestConfigCommand ──────────────────────────────────────────────────────


class TestConfigCommand:
    """Tests for the config command."""

    def test_no_flags_shows_usage(self, runner):
        """No flags shows a helpful message."""
        result = runner.invoke(cli, ["config"])
        assert result.exit_code == 0
        assert "--show" in result.output or "--init" in result.output

    def test_init_creates_config(self, runner):
        """--init creates a new config and prints success."""
        with patch("devils_advocate.cli.init_config", return_value=("created", Path("/fake/models.yaml"))):
            result = runner.invoke(cli, ["config", "--init"])
        assert result.exit_code == 0
        assert "Config created" in result.output

    def test_init_already_exists(self, runner):
        """--init when config already exists prints warning."""
        with patch("devils_advocate.cli.init_config", return_value=("exists", Path("/fake/models.yaml"))):
            result = runner.invoke(cli, ["config", "--init"])
        assert result.exit_code == 0
        assert "already exists" in result.output

    def test_init_with_env_example(self, runner, tmp_path):
        """--init with .env.example present mentions it in output."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        env_example = config_dir / ".env.example"
        env_example.write_text("API_KEY=your-key-here")
        config_file = config_dir / "models.yaml"

        with patch("devils_advocate.cli.init_config", return_value=("created", config_file)):
            result = runner.invoke(cli, ["config", "--init"])
        assert result.exit_code == 0
        assert "Config created" in result.output
        # The .env.example message is conditional on file existence
        assert ".env.example" in result.output or "Edit" in result.output

    def test_show_config_error(self, runner):
        """--show with config error prints error and exits 1."""
        with patch("devils_advocate.cli.load_config", side_effect=ConfigError("No config found")):
            result = runner.invoke(cli, ["config", "--show"])
        assert result.exit_code == 1
        assert "Config error" in result.output
        assert "No config found" in result.output

    def test_show_displays_models_table(self, runner, tmp_path):
        """--show displays configured models in a table."""
        mock_model = MagicMock()
        mock_model.provider = "openai"
        mock_model.model_id = "gpt-4"
        mock_model.roles = {"reviewer"}
        mock_model.deduplication = False
        mock_model.integration_reviewer = False
        mock_model.api_key = "sk-fake"
        mock_model.context_window = 128000
        mock_model.timeout = 120

        mock_config = {
            "models": {"test-model": mock_model},
            "all_models": {"test-model": mock_model},
            "config_path": str(tmp_path / "models.yaml"),
        }

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.validate_config", return_value=[]):
            result = runner.invoke(cli, ["config", "--show"])
        assert result.exit_code == 0
        # Rich may truncate the name in narrow terminals; check for prefix
        assert "test-m" in result.output
        assert "Configuration is valid" in result.output

    def test_show_with_validation_issues(self, runner, tmp_path):
        """--show displays validation issues."""
        mock_model = MagicMock()
        mock_model.provider = "openai"
        mock_model.model_id = "gpt-4"
        mock_model.roles = {"reviewer"}
        mock_model.deduplication = False
        mock_model.integration_reviewer = False
        mock_model.api_key = ""
        mock_model.context_window = None
        mock_model.timeout = 120

        mock_config = {
            "models": {"test-model": mock_model},
            "all_models": {"test-model": mock_model},
            "config_path": str(tmp_path / "models.yaml"),
        }

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.validate_config", return_value=[
                 ("error", "API key missing for test-model"),
                 ("warn", "No context_window set"),
             ]):
            result = runner.invoke(cli, ["config", "--show"])
        assert result.exit_code == 0
        assert "ERROR" in result.output
        assert "API key missing" in result.output
        assert "WARN" in result.output

    def test_show_with_config_path(self, runner, tmp_path):
        """--show --config uses the specified config file."""
        cfg = tmp_path / "models.yaml"
        cfg.write_text("placeholder")
        captured = {}

        def capture_load(path=None):
            captured["path"] = path
            raise ConfigError("test error")

        with patch("devils_advocate.cli.load_config", side_effect=capture_load):
            runner.invoke(cli, ["config", "--show", "--config", str(cfg)])
        assert captured["path"] == cfg

    def test_config_help(self, runner):
        """config --help shows available options."""
        result = runner.invoke(cli, ["config", "--help"])
        assert result.exit_code == 0
        assert "--show" in result.output
        assert "--init" in result.output


# ─── TestOverrideCommand ────────────────────────────────────────────────────


class TestOverrideCommand:
    """Tests for the override command."""

    def test_missing_required_options(self, runner):
        """Missing required options fails."""
        result = runner.invoke(cli, ["override"])
        assert result.exit_code != 0

    def test_successful_uphold_override(self, runner, tmp_path):
        """uphold resolution calls update_point_override with OVERRIDDEN."""
        mock_storage = MagicMock()
        captured = {}

        def capture_update(review_id, point_id, resolution):
            captured["review_id"] = review_id
            captured["point_id"] = point_id
            captured["resolution"] = resolution

        mock_storage.update_point_override = capture_update
        mock_storage.reviews_dir = tmp_path / ".dvad" / "reviews"

        with patch("devils_advocate.cli.StorageManager", return_value=mock_storage):
            result = runner.invoke(cli, [
                "override", "--project", "test",
                "--review", "rev-001", "--point", "pt-001",
                "--resolution", "uphold",
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 0
        assert "Override applied" in result.output
        assert captured["review_id"] == "rev-001"
        assert captured["point_id"] == "pt-001"
        assert captured["resolution"] == Resolution.OVERRIDDEN.value

    def test_successful_dismiss_override(self, runner, tmp_path):
        """dismiss resolution maps to AUTO_DISMISSED."""
        mock_storage = MagicMock()
        captured = {}

        def capture_update(review_id, point_id, resolution):
            captured["resolution"] = resolution

        mock_storage.update_point_override = capture_update
        mock_storage.reviews_dir = tmp_path / ".dvad" / "reviews"

        with patch("devils_advocate.cli.StorageManager", return_value=mock_storage):
            result = runner.invoke(cli, [
                "override", "--project", "test",
                "--review", "rev-001", "--point", "pt-001",
                "--resolution", "dismiss",
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 0
        assert captured["resolution"] == Resolution.AUTO_DISMISSED.value

    def test_successful_escalate_override(self, runner, tmp_path):
        """escalate resolution maps to ESCALATED."""
        mock_storage = MagicMock()
        captured = {}

        def capture_update(review_id, point_id, resolution):
            captured["resolution"] = resolution

        mock_storage.update_point_override = capture_update
        mock_storage.reviews_dir = tmp_path / ".dvad" / "reviews"

        with patch("devils_advocate.cli.StorageManager", return_value=mock_storage):
            result = runner.invoke(cli, [
                "override", "--project", "test",
                "--review", "rev-001", "--point", "pt-001",
                "--resolution", "escalate",
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 0
        assert captured["resolution"] == Resolution.ESCALATED.value

    def test_invalid_resolution_value(self, runner):
        """Invalid resolution value is rejected by Click."""
        result = runner.invoke(cli, [
            "override", "--project", "test",
            "--review", "rev-001", "--point", "pt-001",
            "--resolution", "bogus",
        ])
        assert result.exit_code != 0

    def test_review_not_found(self, runner, tmp_path):
        """StorageError for missing review exits 1."""
        mock_storage = MagicMock()
        mock_storage.update_point_override.side_effect = StorageError("Review rev-999 not found")

        with patch("devils_advocate.cli.StorageManager", return_value=mock_storage):
            result = runner.invoke(cli, [
                "override", "--project", "test",
                "--review", "rev-999", "--point", "pt-001",
                "--resolution", "uphold",
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_point_not_found(self, runner, tmp_path):
        """StorageError for missing point exits 1."""
        mock_storage = MagicMock()
        mock_storage.update_point_override.side_effect = StorageError("Point pt-999 not found")

        with patch("devils_advocate.cli.StorageManager", return_value=mock_storage):
            result = runner.invoke(cli, [
                "override", "--project", "test",
                "--review", "rev-001", "--point", "pt-999",
                "--resolution", "uphold",
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_override_prints_updated_path(self, runner, tmp_path):
        """Successful override prints the updated ledger path."""
        mock_storage = MagicMock()
        mock_storage.reviews_dir = tmp_path / ".dvad" / "reviews"

        with patch("devils_advocate.cli.StorageManager", return_value=mock_storage):
            result = runner.invoke(cli, [
                "override", "--project", "test",
                "--review", "rev-001", "--point", "pt-001",
                "--resolution", "uphold",
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 0
        assert "review-ledger.json" in result.output

    def test_override_help(self, runner):
        """override --help shows the resolution descriptions."""
        result = runner.invoke(cli, ["override", "--help"])
        assert result.exit_code == 0
        assert "uphold" in result.output
        assert "dismiss" in result.output
        assert "escalate" in result.output


# ─── TestReviseCommand ──────────────────────────────────────────────────────


class TestReviseCommand:
    """Tests for the revise command."""

    def _make_revise_mocks(self, tmp_path, mode="plan", ledger_data=None):
        """Build common mocks for revise tests, returns (mock_config, mock_roles, mock_storage)."""
        mock_config = {"models": {}, "config_path": "/fake"}
        mock_model = MagicMock()
        mock_roles = {"revision": mock_model}
        mock_storage = MagicMock()
        mock_storage.load_review.return_value = ledger_data or {"mode": mode}
        mock_storage.reviews_dir = tmp_path / ".dvad" / "reviews"
        return mock_config, mock_roles, mock_storage

    def _create_review_dir(self, tmp_path, review_id="rev-001", original_content="content"):
        """Create the review directory with original_content.txt."""
        rd = tmp_path / ".dvad" / "reviews" / review_id
        rd.mkdir(parents=True, exist_ok=True)
        if original_content is not None:
            (rd / "original_content.txt").write_text(original_content)
        return rd

    def test_missing_required_options(self, runner):
        """Missing project and review exits with error."""
        result = runner.invoke(cli, ["revise"])
        assert result.exit_code != 0

    def test_config_load_error_exits_1(self, runner):
        """ConfigError on load exits 1."""
        with patch("devils_advocate.cli.load_config", side_effect=ConfigError("bad config")):
            result = runner.invoke(cli, [
                "revise", "--project", "test", "--review", "rev-001",
            ])
        assert result.exit_code == 1
        assert "Config error" in result.output

    def test_review_not_found_exits_1(self, runner, tmp_path):
        """Non-existent review exits 1."""
        mock_config, mock_roles, mock_storage = self._make_revise_mocks(tmp_path)
        mock_storage.load_review.return_value = None

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.get_models_by_role", return_value=mock_roles), \
             patch("devils_advocate.cli.StorageManager", return_value=mock_storage):
            result = runner.invoke(cli, [
                "revise", "--project", "test", "--review", "rev-999",
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_missing_original_content_exits_1(self, runner, tmp_path):
        """Missing original_content.txt without --input exits 1."""
        mock_config, mock_roles, mock_storage = self._make_revise_mocks(tmp_path)
        # Create review directory but NOT original_content.txt
        self._create_review_dir(tmp_path, original_content=None)

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.get_models_by_role", return_value=mock_roles), \
             patch("devils_advocate.cli.StorageManager", return_value=mock_storage):
            result = runner.invoke(cli, [
                "revise", "--project", "test", "--review", "rev-001",
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 1
        assert "original_content.txt not found" in result.output

    def test_input_override_file_not_found(self, runner, tmp_path):
        """--input with non-existent file exits 1."""
        mock_config, mock_roles, mock_storage = self._make_revise_mocks(tmp_path)

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.get_models_by_role", return_value=mock_roles), \
             patch("devils_advocate.cli.StorageManager", return_value=mock_storage):
            result = runner.invoke(cli, [
                "revise", "--project", "test", "--review", "rev-001",
                "--input", str(tmp_path / "missing.md"),
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 1
        assert "Input file not found" in result.output

    def test_successful_revision_plan_mode(self, runner, tmp_path):
        """Successful plan revision writes revised-plan.md and prints completion."""
        mock_config, mock_roles, mock_storage = self._make_revise_mocks(tmp_path, mode="plan")
        self._create_review_dir(tmp_path, original_content="# Original Plan")

        async def fake_revision(*a, **kw):
            return "# Revised Plan\nAll findings addressed."

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.get_models_by_role", return_value=mock_roles), \
             patch("devils_advocate.cli.StorageManager", return_value=mock_storage), \
             patch("devils_advocate.cli.run_revision", side_effect=fake_revision):
            result = runner.invoke(cli, [
                "revise", "--project", "test", "--review", "rev-001",
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 0
        assert "Revision complete" in result.output
        assert "revised-plan.md" in result.output
        mock_storage._atomic_write.assert_called_once()

    def test_revision_code_mode_output_name(self, runner, tmp_path):
        """Code mode revision outputs revised-diff.patch."""
        mock_config, mock_roles, mock_storage = self._make_revise_mocks(tmp_path, mode="code")
        self._create_review_dir(tmp_path, original_content="def foo(): pass")

        async def fake_revision(*a, **kw):
            return "--- a/main.py\n+++ b/main.py"

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.get_models_by_role", return_value=mock_roles), \
             patch("devils_advocate.cli.StorageManager", return_value=mock_storage), \
             patch("devils_advocate.cli.run_revision", side_effect=fake_revision):
            result = runner.invoke(cli, [
                "revise", "--project", "test", "--review", "rev-001",
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 0
        assert "revised-diff.patch" in result.output

    def test_revision_integration_mode_output_name(self, runner, tmp_path):
        """Integration mode revision outputs remediation-plan.md."""
        mock_config, mock_roles, mock_storage = self._make_revise_mocks(tmp_path, mode="integration")
        self._create_review_dir(tmp_path, original_content="integration content")

        async def fake_revision(*a, **kw):
            return "# Remediation Plan"

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.get_models_by_role", return_value=mock_roles), \
             patch("devils_advocate.cli.StorageManager", return_value=mock_storage), \
             patch("devils_advocate.cli.run_revision", side_effect=fake_revision):
            result = runner.invoke(cli, [
                "revise", "--project", "test", "--review", "rev-001",
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 0
        assert "remediation-plan.md" in result.output

    def test_revision_unknown_mode_defaults_to_plan(self, runner, tmp_path):
        """Unknown mode defaults to revised-plan.md output name."""
        mock_config, mock_roles, mock_storage = self._make_revise_mocks(tmp_path, mode="unusual")
        self._create_review_dir(tmp_path, original_content="content")

        async def fake_revision(*a, **kw):
            return "# Revised"

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.get_models_by_role", return_value=mock_roles), \
             patch("devils_advocate.cli.StorageManager", return_value=mock_storage), \
             patch("devils_advocate.cli.run_revision", side_effect=fake_revision):
            result = runner.invoke(cli, [
                "revise", "--project", "test", "--review", "rev-001",
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 0
        assert "revised-plan.md" in result.output

    def test_revision_returns_none(self, runner, tmp_path):
        """Revision returning None prints no-artifact message."""
        mock_config, mock_roles, mock_storage = self._make_revise_mocks(tmp_path)
        self._create_review_dir(tmp_path, original_content="content")

        async def fake_revision(*a, **kw):
            return None

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.get_models_by_role", return_value=mock_roles), \
             patch("devils_advocate.cli.StorageManager", return_value=mock_storage), \
             patch("devils_advocate.cli.run_revision", side_effect=fake_revision):
            result = runner.invoke(cli, [
                "revise", "--project", "test", "--review", "rev-001",
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 0
        assert "No revised artifact produced" in result.output

    def test_revision_returns_empty_string(self, runner, tmp_path):
        """Revision returning empty string is falsy, prints no-artifact."""
        mock_config, mock_roles, mock_storage = self._make_revise_mocks(tmp_path)
        self._create_review_dir(tmp_path, original_content="content")

        async def fake_revision(*a, **kw):
            return ""

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.get_models_by_role", return_value=mock_roles), \
             patch("devils_advocate.cli.StorageManager", return_value=mock_storage), \
             patch("devils_advocate.cli.run_revision", side_effect=fake_revision):
            result = runner.invoke(cli, [
                "revise", "--project", "test", "--review", "rev-001",
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 0
        assert "No revised artifact produced" in result.output

    def test_revision_api_error_exits_1(self, runner, tmp_path):
        """APIError during revision exits 1."""
        mock_config, mock_roles, mock_storage = self._make_revise_mocks(tmp_path)
        self._create_review_dir(tmp_path, original_content="content")

        async def raise_api(*a, **kw):
            raise APIError("API call failed")

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.get_models_by_role", return_value=mock_roles), \
             patch("devils_advocate.cli.StorageManager", return_value=mock_storage), \
             patch("devils_advocate.cli.run_revision", side_effect=raise_api):
            result = runner.invoke(cli, [
                "revise", "--project", "test", "--review", "rev-001",
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 1
        assert "API call failed" in result.output

    def test_revision_cost_limit_error_exits_1(self, runner, tmp_path):
        """CostLimitError during revision exits 1."""
        mock_config, mock_roles, mock_storage = self._make_revise_mocks(tmp_path)
        self._create_review_dir(tmp_path, original_content="content")

        async def raise_cost(*a, **kw):
            raise CostLimitError("Over budget")

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.get_models_by_role", return_value=mock_roles), \
             patch("devils_advocate.cli.StorageManager", return_value=mock_storage), \
             patch("devils_advocate.cli.run_revision", side_effect=raise_cost):
            result = runner.invoke(cli, [
                "revise", "--project", "test", "--review", "rev-001",
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 1
        assert "Over budget" in result.output

    def test_revision_generic_exception_nonfatal(self, runner, tmp_path):
        """Generic Exception during revision is non-fatal (warning printed, exit 0)."""
        mock_config, mock_roles, mock_storage = self._make_revise_mocks(tmp_path)
        self._create_review_dir(tmp_path, original_content="content")

        async def raise_generic(*a, **kw):
            raise RuntimeError("Unexpected issue")

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.get_models_by_role", return_value=mock_roles), \
             patch("devils_advocate.cli.StorageManager", return_value=mock_storage), \
             patch("devils_advocate.cli.run_revision", side_effect=raise_generic):
            result = runner.invoke(cli, [
                "revise", "--project", "test", "--review", "rev-001",
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 0
        assert "Revision failed" in result.output
        # Storage.log should be called for non-fatal failures
        mock_storage.log.assert_called_once()

    def test_revision_with_input_override(self, runner, tmp_path):
        """--input override reads content from the specified file."""
        mock_config, mock_roles, mock_storage = self._make_revise_mocks(tmp_path)
        # Create review dir WITHOUT original_content.txt
        rd = tmp_path / ".dvad" / "reviews" / "rev-001"
        rd.mkdir(parents=True)

        override_file = tmp_path / "override.md"
        override_file.write_text("# Override Content")

        captured = {}

        async def fake_revision(client, revision_model, original_content, ledger_data, **kw):
            captured["original_content"] = original_content
            return "# Revised"

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.get_models_by_role", return_value=mock_roles), \
             patch("devils_advocate.cli.StorageManager", return_value=mock_storage), \
             patch("devils_advocate.cli.run_revision", side_effect=fake_revision):
            result = runner.invoke(cli, [
                "revise", "--project", "test", "--review", "rev-001",
                "--input", str(override_file),
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 0
        assert captured["original_content"] == "# Override Content"

    def test_revision_max_cost_forwarded(self, runner, tmp_path):
        """--max-cost is forwarded to CostTracker."""
        mock_config, mock_roles, mock_storage = self._make_revise_mocks(tmp_path)
        self._create_review_dir(tmp_path, original_content="content")

        captured = {}

        async def fake_revision(*a, **kw):
            captured["cost_tracker"] = kw.get("cost_tracker")
            return "# Revised"

        with patch("devils_advocate.cli.load_config", return_value=mock_config), \
             patch("devils_advocate.cli.get_models_by_role", return_value=mock_roles), \
             patch("devils_advocate.cli.StorageManager", return_value=mock_storage), \
             patch("devils_advocate.cli.run_revision", side_effect=fake_revision):
            result = runner.invoke(cli, [
                "revise", "--project", "test", "--review", "rev-001",
                "--max-cost", "3.50",
                "--project-dir", str(tmp_path),
            ])
        assert result.exit_code == 0
        assert captured["cost_tracker"].max_cost == 3.50

    def test_revise_help(self, runner):
        """revise --help shows expected options."""
        result = runner.invoke(cli, ["revise", "--help"])
        assert result.exit_code == 0
        assert "--project" in result.output
        assert "--review" in result.output
        assert "--max-cost" in result.output
        assert "--input" in result.output


# ─── TestGuiCommand ─────────────────────────────────────────────────────────


def _mock_gui_context(mock_sock_instance, mock_uvicorn=None, mock_create_app=None):
    """Context manager helper for GUI tests that need socket and import mocking.

    The gui_cmd function uses local imports:
      ``import socket`` and ``import uvicorn`` and ``from .gui import create_app``
    Since socket is imported locally, we patch the global socket module's socket class.
    For uvicorn and gui, we patch sys.modules so local imports pick up our mocks.
    """
    gui_mock = MagicMock()
    if mock_create_app is not None:
        gui_mock.create_app = mock_create_app
    else:
        gui_mock.create_app = MagicMock(return_value=MagicMock())

    modules_patch = {"devils_advocate.gui": gui_mock}
    if mock_uvicorn is not None:
        modules_patch["uvicorn"] = mock_uvicorn

    return patch.dict("sys.modules", modules_patch), \
        patch("socket.socket", return_value=mock_sock_instance)


class TestGuiCommand:
    """Tests for the gui command."""

    def test_gui_help(self, runner):
        """gui --help shows usage."""
        result = runner.invoke(cli, ["gui", "--help"])
        assert result.exit_code == 0
        assert "Launch" in result.output
        assert "--port" in result.output
        assert "--host" in result.output
        assert "--allow-nonlocal" in result.output

    def test_gui_default_port_in_help(self, runner):
        """Port option is documented in help."""
        result = runner.invoke(cli, ["gui", "--help"])
        assert result.exit_code == 0
        assert "--port" in result.output
        assert "Port to listen on" in result.output

    def test_port_in_use_exits_1(self, runner):
        """Port already in use prints error and exits 1."""
        mock_sock = MagicMock()
        mock_sock.bind.side_effect = OSError("Address already in use")

        modules_patch, socket_patch = _mock_gui_context(mock_sock)
        with modules_patch, socket_patch:
            result = runner.invoke(cli, ["gui", "--port", "9999"])
        assert result.exit_code == 1
        assert "already in use" in result.output

    def test_port_in_use_suggests_next_port(self, runner):
        """Port-in-use error message suggests port+1."""
        mock_sock = MagicMock()
        mock_sock.bind.side_effect = OSError("Address already in use")

        modules_patch, socket_patch = _mock_gui_context(mock_sock)
        with modules_patch, socket_patch:
            result = runner.invoke(cli, ["gui", "--port", "9999"])
        assert result.exit_code == 1
        assert "10000" in result.output

    def test_nonlocal_binding_refused_without_flag(self, runner):
        """Non-localhost binding without --allow-nonlocal exits 1."""
        mock_sock = MagicMock()

        modules_patch, socket_patch = _mock_gui_context(mock_sock)
        with modules_patch, socket_patch:
            result = runner.invoke(cli, [
                "gui", "--host", "0.0.0.0", "--port", "18411",
            ])
        assert result.exit_code == 1
        assert "Refusing" in result.output

    def test_nonlocal_with_allow_flag_proceeds(self, runner):
        """--allow-nonlocal permits non-localhost binding and shows warning."""
        mock_sock = MagicMock()
        mock_uvicorn = MagicMock()
        mock_app = MagicMock()

        modules_patch, socket_patch = _mock_gui_context(
            mock_sock,
            mock_uvicorn=mock_uvicorn,
            mock_create_app=MagicMock(return_value=mock_app),
        )
        with modules_patch, socket_patch:
            result = runner.invoke(cli, [
                "gui", "--host", "0.0.0.0", "--port", "18411",
                "--allow-nonlocal",
            ])
        assert result.exit_code == 0
        assert "Warning" in result.output
        mock_uvicorn.run.assert_called_once()

    def test_successful_localhost_launch(self, runner):
        """Successful localhost GUI launch calls uvicorn.run."""
        mock_sock = MagicMock()
        mock_uvicorn = MagicMock()
        mock_app = MagicMock()
        mock_create_app = MagicMock(return_value=mock_app)

        modules_patch, socket_patch = _mock_gui_context(
            mock_sock,
            mock_uvicorn=mock_uvicorn,
            mock_create_app=mock_create_app,
        )
        with modules_patch, socket_patch:
            result = runner.invoke(cli, ["gui", "--port", "18412"])
        assert result.exit_code == 0
        mock_uvicorn.run.assert_called_once_with(
            mock_app, host="127.0.0.1", port=18412, log_level="warning",
        )

    def test_gui_import_error(self, runner):
        """Missing GUI dependencies print install instructions and exit 1."""
        # Set the gui module to None in sys.modules so the relative import fails
        # with ImportError (Python treats None in sys.modules as ImportError)
        with patch.dict("sys.modules", {"devils_advocate.gui": None}):
            import builtins
            original_import = builtins.__import__

            def fail_gui_import(name, globals=None, locals=None, fromlist=(), level=0):
                if level > 0 and fromlist and "create_app" in fromlist:
                    raise ImportError("No module named 'devils_advocate.gui'")
                return original_import(name, globals, locals, fromlist, level)

            with patch("builtins.__import__", side_effect=fail_gui_import):
                result = runner.invoke(cli, ["gui"])

        assert result.exit_code == 1
        assert "GUI dependencies not installed" in result.output

    def test_gui_config_path_forwarded(self, runner):
        """--config is forwarded to create_app."""
        mock_sock = MagicMock()
        mock_uvicorn = MagicMock()
        mock_create_app = MagicMock(return_value=MagicMock())

        modules_patch, socket_patch = _mock_gui_context(
            mock_sock,
            mock_uvicorn=mock_uvicorn,
            mock_create_app=mock_create_app,
        )
        with modules_patch, socket_patch:
            result = runner.invoke(cli, [
                "gui", "--config", "/path/to/models.yaml",
            ])
        assert result.exit_code == 0
        mock_create_app.assert_called_once_with(config_path="/path/to/models.yaml")
