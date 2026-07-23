"""Tests for cli/_common.py wiring helpers.

Covers the bridge from .devrel/config.toml into Atlas's runtime AgentConfig
plus the env/config resolution for GitHub repo. These helpers exist because
build_atlas_or_exit was previously passing only 4 of Atlas's 9 init args,
silently dropping the user's [orchestration].agent_timeouts and forcing
GitHub-using specialists into the default OpenClaw repo.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from devrel_origin.cli._common import (
    _load_agent_config,
    _resolve_github_repo,
    build_atlas_or_exit,
)
from devrel_origin.project.paths import ProjectPaths


def _make_paths(root: Path) -> ProjectPaths:
    devrel = root / ".devrel"
    devrel.mkdir(parents=True, exist_ok=True)
    return ProjectPaths.from_root(root)


class TestLoadAgentConfig:
    def test_returns_defaults_when_no_config_file(self, tmp_path):
        paths = _make_paths(tmp_path)
        # Note: no config.toml written
        config = _load_agent_config(paths)
        assert config.product_name  # whatever the AgentConfig default resolves to
        assert config.agent_timeouts == {}
        assert config.cro_in_run is False
        assert config.analytics_in_run is True

    def test_reads_project_identity(self, tmp_path):
        paths = _make_paths(tmp_path)
        paths.config_file.write_text('[project]\nname = "Example"\nurl = "https://example.com"\n')
        config = _load_agent_config(paths)
        assert config.product_name == "Example"
        assert config.product_url == "https://example.com"

    def test_reads_orchestration_agent_timeouts(self, tmp_path):
        paths = _make_paths(tmp_path)
        paths.config_file.write_text(
            '[project]\nname = "X"\n\n[orchestration.agent_timeouts]\nkai = 1200.0\nsage = 60.0\n'
        )
        config = _load_agent_config(paths)
        assert config.agent_timeouts == {"kai": 1200.0, "sage": 60.0}

    def test_reads_orchestration_in_run_flags(self, tmp_path):
        paths = _make_paths(tmp_path)
        paths.config_file.write_text(
            '[project]\nname = "X"\n\n'
            "[orchestration]\n"
            "cro_in_run = true\n"
            "analytics_in_run = false\n"
        )
        config = _load_agent_config(paths)
        assert config.cro_in_run is True
        assert config.analytics_in_run is False

    def test_malformed_toml_falls_back_to_defaults(self, tmp_path):
        paths = _make_paths(tmp_path)
        paths.config_file.write_text("this is not valid toml [[[\n")
        # Should not raise; returns defaults.
        config = _load_agent_config(paths)
        assert config.agent_timeouts == {}


class TestResolveGithubRepo:
    def test_env_wins_over_toml(self, tmp_path):
        paths = _make_paths(tmp_path)
        paths.config_file.write_text('[project]\nname = "X"\ngithub_repo = "from/toml"\n')
        with patch.dict(os.environ, {"GITHUB_REPO": "from/env"}):
            assert _resolve_github_repo(paths) == "from/env"

    def test_toml_used_when_no_env(self, tmp_path):
        paths = _make_paths(tmp_path)
        paths.config_file.write_text('[project]\nname = "X"\ngithub_repo = "PostHog/posthog"\n')
        # Drop GITHUB_REPO so env path doesn't win
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_REPO", None)
            assert _resolve_github_repo(paths) == "PostHog/posthog"

    def test_empty_when_neither_set(self, tmp_path):
        paths = _make_paths(tmp_path)
        paths.config_file.write_text('[project]\nname = "X"\n')
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_REPO", None)
            assert _resolve_github_repo(paths) == ""

    def test_no_config_file_returns_empty(self, tmp_path):
        paths = _make_paths(tmp_path)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_REPO", None)
            assert _resolve_github_repo(paths) == ""


class TestBuildAtlasWiring:
    """Atlas should receive archive_dir, config, and tool clients matching env+toml."""

    def test_atlas_gets_context_dir_as_archive_dir(self, tmp_path, capsys):
        paths = _make_paths(tmp_path)
        paths.config_file.write_text('[project]\nname = "X"\n')
        from rich.console import Console

        console = Console()
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "test",
                "POSTHOG_API_KEY": "",
                "POSTHOG_PROJECT_ID": "",
            },
            clear=False,
        ):
            for k in (
                "GITHUB_TOKEN",
                "FIRECRAWL_API_KEY",
                "BRAVE_API_KEY",
                "INSTANTLY_API_KEY",
                "APOLLO_API_KEY",
            ):
                os.environ.pop(k, None)
            with patch("devrel_origin.cli._common.Atlas") as MockAtlas:
                build_atlas_or_exit(paths, console)
                kwargs = MockAtlas.call_args.kwargs
                assert kwargs["archive_dir"] == paths.context_dir
                assert kwargs["github_tools"] is None
                assert kwargs["search_tools"] is None
                assert kwargs["instantly_client"] is None
                assert kwargs["apollo_client"] is None
                # Config bridged from .toml; defaults but real AgentConfig instance
                assert kwargs["config"].product_name == "X"

    def test_atlas_gets_config_with_agent_timeouts_from_toml(self, tmp_path):
        paths = _make_paths(tmp_path)
        paths.config_file.write_text(
            '[project]\nname = "X"\n\n[orchestration.agent_timeouts]\nkai = 900.0\n'
        )
        from rich.console import Console

        console = Console()
        with patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "test", "POSTHOG_API_KEY": "", "POSTHOG_PROJECT_ID": ""},
            clear=False,
        ):
            for k in (
                "GITHUB_TOKEN",
                "FIRECRAWL_API_KEY",
                "BRAVE_API_KEY",
                "INSTANTLY_API_KEY",
                "APOLLO_API_KEY",
            ):
                os.environ.pop(k, None)
            with patch("devrel_origin.cli._common.Atlas") as MockAtlas:
                build_atlas_or_exit(paths, console)
                config = MockAtlas.call_args.kwargs["config"]
                assert config.agent_timeouts == {"kai": 900.0}

    def test_atlas_gets_github_tools_when_token_set(self, tmp_path):
        paths = _make_paths(tmp_path)
        paths.config_file.write_text('[project]\nname = "X"\ngithub_repo = "PostHog/posthog"\n')
        from rich.console import Console

        console = Console()
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "test",
                "POSTHOG_API_KEY": "",
                "POSTHOG_PROJECT_ID": "",
                "GITHUB_TOKEN": "ghp_test",
            },
            clear=False,
        ):
            for k in (
                "FIRECRAWL_API_KEY",
                "BRAVE_API_KEY",
                "INSTANTLY_API_KEY",
                "APOLLO_API_KEY",
                "GITHUB_REPO",
            ):
                os.environ.pop(k, None)
            with patch("devrel_origin.cli._common.Atlas") as MockAtlas:
                build_atlas_or_exit(paths, console)
                gh = MockAtlas.call_args.kwargs["github_tools"]
                assert gh is not None
                assert gh.repo == "PostHog/posthog"

    def test_atlas_gets_public_github_tools_when_repo_set_without_token(self, tmp_path):
        paths = _make_paths(tmp_path)
        paths.config_file.write_text('[project]\nname = "X"\ngithub_repo = "PostHog/posthog"\n')
        from rich.console import Console

        console = Console()
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "test",
                "POSTHOG_API_KEY": "",
                "POSTHOG_PROJECT_ID": "",
            },
            clear=False,
        ):
            for k in (
                "GITHUB_TOKEN",
                "FIRECRAWL_API_KEY",
                "BRAVE_API_KEY",
                "INSTANTLY_API_KEY",
                "APOLLO_API_KEY",
                "GITHUB_REPO",
            ):
                os.environ.pop(k, None)
            with patch("devrel_origin.cli._common.Atlas") as MockAtlas:
                build_atlas_or_exit(paths, console)
                gh = MockAtlas.call_args.kwargs["github_tools"]
                assert gh is not None
                assert gh.repo == "PostHog/posthog"

    def test_atlas_gets_search_tools_when_firecrawl_set(self, tmp_path):
        paths = _make_paths(tmp_path)
        paths.config_file.write_text('[project]\nname = "X"\n')
        from rich.console import Console

        console = Console()
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "test",
                "POSTHOG_API_KEY": "",
                "POSTHOG_PROJECT_ID": "",
                "FIRECRAWL_API_KEY": "fc_test",
            },
            clear=False,
        ):
            for k in (
                "GITHUB_TOKEN",
                "BRAVE_API_KEY",
                "INSTANTLY_API_KEY",
                "APOLLO_API_KEY",
            ):
                os.environ.pop(k, None)
            with patch("devrel_origin.cli._common.Atlas") as MockAtlas:
                build_atlas_or_exit(paths, console)
                st = MockAtlas.call_args.kwargs["search_tools"]
                assert st is not None

    def test_atlas_exits_without_anthropic_key(self, tmp_path):
        import typer

        paths = _make_paths(tmp_path)
        paths.config_file.write_text('[project]\nname = "X"\n')
        from rich.console import Console

        console = Console()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("OPENROUTER_API_KEY", None)
            with pytest.raises(typer.Exit):
                build_atlas_or_exit(paths, console)


class TestLLMClientWiring:
    """Provider selection + per-agent model overrides from .devrel/config.toml."""

    def test_openrouter_selected_when_provider_explicitly_set(self, tmp_path):
        from devrel_origin.cli._common import _build_llm_client

        paths = _make_paths(tmp_path)
        paths.config_file.write_text('[project]\nname = "X"\n\n[llm]\nprovider = "openrouter"\n')
        from rich.console import Console

        console = Console()
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "k_or"}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            client = _build_llm_client(paths, console)
        assert client.backend.name == "openrouter"

    def test_openrouter_selected_when_only_or_key_in_env(self, tmp_path):
        from devrel_origin.cli._common import _build_llm_client

        paths = _make_paths(tmp_path)
        paths.config_file.write_text('[project]\nname = "X"\n')
        from rich.console import Console

        console = Console()
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "k_or"}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            client = _build_llm_client(paths, console)
        assert client.backend.name == "openrouter"

    def test_anthropic_selected_when_both_env_keys_present(self, tmp_path):
        from devrel_origin.cli._common import _build_llm_client

        paths = _make_paths(tmp_path)
        paths.config_file.write_text('[project]\nname = "X"\n')
        from rich.console import Console

        console = Console()
        with patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "k_a", "OPENROUTER_API_KEY": "k_or"},
            clear=False,
        ):
            client = _build_llm_client(paths, console)
        assert client.backend.name == "anthropic"

    def test_agent_models_passed_through(self, tmp_path):
        from devrel_origin.cli._common import _build_llm_client

        paths = _make_paths(tmp_path)
        paths.config_file.write_text(
            '[project]\nname = "X"\n\n'
            "[llm.agent_models]\n"
            'argus = "openai/gpt-4o-mini"\n'
            'kai = "anthropic/claude-opus-4-0-20250514"\n'
        )
        from rich.console import Console

        console = Console()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}, clear=False):
            os.environ.pop("OPENROUTER_API_KEY", None)
            client = _build_llm_client(paths, console)
        assert client.agent_models == {
            "argus": "openai/gpt-4o-mini",
            "kai": "anthropic/claude-opus-4-0-20250514",
        }

    def test_exits_when_provider_openrouter_but_no_or_key(self, tmp_path):
        import typer

        from devrel_origin.cli._common import _build_llm_client

        paths = _make_paths(tmp_path)
        paths.config_file.write_text('[project]\nname = "X"\n\n[llm]\nprovider = "openrouter"\n')
        from rich.console import Console

        console = Console()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("OPENROUTER_API_KEY", None)
            with pytest.raises(typer.Exit):
                _build_llm_client(paths, console)


class TestEnvAutoLoad:
    """Keys in .devrel/.env or project-root .env should populate os.environ
    before _build_llm_client reads them, without overriding shell exports."""

    def test_loads_devrel_env_when_present(self, tmp_path):
        from devrel_origin.cli._common import _build_llm_client

        paths = _make_paths(tmp_path)
        paths.config_file.write_text('[project]\nname = "X"\n')
        paths.env_file.write_text("ANTHROPIC_API_KEY=sk-ant-from-file\n")
        from rich.console import Console

        console = Console()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("OPENROUTER_API_KEY", None)
            client = _build_llm_client(paths, console)
        assert client.backend.name == "anthropic"

    def test_loads_root_env_as_fallback(self, tmp_path):
        from devrel_origin.cli._common import _build_llm_client

        paths = _make_paths(tmp_path)
        paths.config_file.write_text('[project]\nname = "X"\n')
        # Note: writing to project root, not .devrel/.env
        (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-from-root\n")
        from rich.console import Console

        console = Console()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("OPENROUTER_API_KEY", None)
            client = _build_llm_client(paths, console)
        assert client.backend.name == "openrouter"

    def test_shell_export_wins_over_dotenv(self, tmp_path):
        """python-dotenv override=False: a key already in the shell env wins
        over the file. Lets users debug-override with a one-shot
        `ANTHROPIC_API_KEY=other-key devrel run` without editing the file."""
        from devrel_origin.cli._common import _build_llm_client

        paths = _make_paths(tmp_path)
        paths.config_file.write_text('[project]\nname = "X"\n')
        paths.env_file.write_text("ANTHROPIC_API_KEY=from-file\n")
        from rich.console import Console

        console = Console()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "from-shell"}, clear=False):
            os.environ.pop("OPENROUTER_API_KEY", None)
            client = _build_llm_client(paths, console)
            assert client.backend.name == "anthropic"
            # The shell-exported value must survive _load_project_env;
            # python-dotenv with override=False is the load path that
            # guarantees this.
            assert os.environ["ANTHROPIC_API_KEY"] == "from-shell"
