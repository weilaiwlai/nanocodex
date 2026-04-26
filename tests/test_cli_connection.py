"""Tests for CLI connection error handling.

This module tests the scenario where the OpenAI API connection fails,
verifying that the CLI properly catches and reports the connection error.
"""

from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

AGENT_CODE_ROOT = Path(__file__).resolve().parent.parent
if str(AGENT_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_CODE_ROOT))


def _create_mock_package(*submodules):
    mock_pkg = ModuleType(submodules[0])
    mock_pkg.__path__ = []
    sys.modules[submodules[0]] = mock_pkg
    current = mock_pkg
    for sm in submodules[1:]:
        sm_mod = ModuleType(f"{current.__name__}.{sm}")
        setattr(current, sm, sm_mod)
        sys.modules[f"{current.__name__}.{sm}"] = sm_mod
        current = sm_mod
    return mock_pkg


def _create_mock_prompt_toolkit():
    mock_pt = ModuleType("prompt_toolkit")
    mock_pt.__path__ = []
    sys.modules["prompt_toolkit"] = mock_pt

    mock_history = ModuleType("prompt_toolkit.history")
    mock_history.__path__ = []
    mock_pt.history = mock_history
    sys.modules["prompt_toolkit.history"] = mock_history

    mock_key_binding = ModuleType("prompt_toolkit.key_binding")
    mock_key_binding.__path__ = []
    mock_pt.key_binding = mock_key_binding
    sys.modules["prompt_toolkit.key_binding"] = mock_key_binding

    return mock_pt


def _create_mock_agents():
    mock_agents = ModuleType("agents")
    mock_agents.__path__ = []
    sys.modules["agents"] = mock_agents

    mock_items = ModuleType("agents.items")
    mock_items.__path__ = []
    mock_agents.items = mock_items
    sys.modules["agents.items"] = mock_items

    return mock_agents


def _create_mock_openai():
    mock_openai = ModuleType("openai")
    mock_openai.__path__ = []
    sys.modules["openai"] = mock_openai

    mock_types = ModuleType("openai.types")
    mock_types.__path__ = []
    mock_openai.types = mock_types
    sys.modules["openai.types"] = mock_types

    mock_responses = ModuleType("openai.types.responses")
    mock_responses.__path__ = []
    mock_types.responses = mock_responses
    sys.modules["openai.types.responses"] = mock_responses

    return mock_openai


class TestPrintConnectionError:
    """Test suite for print_connection_error function."""

    def test_print_connection_error_outputs_correct_message(self):
        """Verify that print_connection_error prints the expected error message."""
        mock_pt = _create_mock_prompt_toolkit()
        mock_pt.PromptSession = MagicMock()
        mock_pt.history.FileHistory = MagicMock()
        mock_pt.key_binding.KeyBindings = MagicMock()

        mock_openai = _create_mock_openai()
        mock_openai.APIConnectionError = MagicMock()
        mock_openai.AsyncOpenAI = MagicMock()
        mock_openai.types.responses.ResponseTextDeltaEvent = MagicMock()

        mock_agents = _create_mock_agents()
        mock_agents.Runner = MagicMock()
        mock_agents.RunConfig = MagicMock()
        mock_agents.Agent = MagicMock()
        mock_agents.set_default_openai_api = MagicMock()
        mock_agents.set_default_openai_client = MagicMock()
        mock_agents.set_tracing_disabled = MagicMock()
        mock_agents.TResponseInputItem = MagicMock()
        mock_agents.RunContextWrapper = MagicMock()
        mock_agents.function_tool = MagicMock()
        mock_agents.SQLiteSession = MagicMock()
        mock_agents.items.ToolCallItem = MagicMock()
        mock_agents.items.ToolCallOutputItem = MagicMock()

        mock_tiktoken = MagicMock()
        sys.modules["tiktoken"] = mock_tiktoken

        mock_src_context = MagicMock()
        sys.modules["src.context"] = mock_src_context
        sys.modules["src.context.context_builder"] = MagicMock()
        sys.modules["src.context.compaction"] = MagicMock()
        sys.modules["src.runtime.agent_factory"] = MagicMock()

        with patch.dict("sys.modules", {
            "prompt_toolkit": mock_pt,
            "openai": mock_openai,
            "agents": mock_agents,
        }, clear=False):
            if "scripts.cli" in sys.modules:
                del sys.modules["scripts.cli"]
            for mod in list(sys.modules.keys()):
                if mod.startswith("src.runtime"):
                    del sys.modules[mod]
            from scripts.cli import print_connection_error

        stderr_capture = StringIO()
        with patch("sys.stderr", stderr_capture):
            print_connection_error()

        output = stderr_capture.getvalue()
        assert "模型连接失败" in output
        assert "OPENAI_BASE_URL" in output
        assert "请检查网络" in output


class TestStreamReply:
    """Test suite for stream_reply function."""

    def test_stream_reply_catches_api_connection_error(self):
        """Verify that stream_reply properly catches APIConnectionError."""
        mock_pt = _create_mock_prompt_toolkit()
        mock_pt.PromptSession = MagicMock()
        mock_pt.history.FileHistory = MagicMock()
        mock_pt.key_binding.KeyBindings = MagicMock()

        mock_openai = _create_mock_openai()
        mock_openai.APIConnectionError = Exception
        mock_openai.AsyncOpenAI = MagicMock()
        mock_openai.types.responses.ResponseTextDeltaEvent = MagicMock()

        mock_agents = _create_mock_agents()
        mock_agents.Runner = MagicMock()
        mock_agents.RunConfig = MagicMock()
        mock_agents.Agent = MagicMock()
        mock_agents.set_default_openai_api = MagicMock()
        mock_agents.set_default_openai_client = MagicMock()
        mock_agents.set_tracing_disabled = MagicMock()
        mock_agents.TResponseInputItem = MagicMock()
        mock_agents.RunContextWrapper = MagicMock()
        mock_agents.function_tool = MagicMock()
        mock_agents.SQLiteSession = MagicMock()
        mock_agents.items.ToolCallItem = MagicMock()
        mock_agents.items.ToolCallOutputItem = MagicMock()

        mock_tiktoken = MagicMock()
        sys.modules["tiktoken"] = mock_tiktoken

        mock_src_context = MagicMock()
        sys.modules["src.context"] = mock_src_context
        sys.modules["src.context.context_builder"] = MagicMock()
        sys.modules["src.context.compaction"] = MagicMock()
        sys.modules["src.runtime.agent_factory"] = MagicMock()
        sys.modules["src.tools.registry"] = MagicMock()
        sys.modules["src.tools.bash_tool"] = MagicMock()
        sys.modules["src.tools.worktree_tools"] = MagicMock()
        sys.modules["src.tools.todo_write"] = MagicMock()
        sys.modules["src.tools.team_tools"] = MagicMock()
        sys.modules["src.tools.task_tools"] = MagicMock()
        sys.modules["src.tools.skill_tool"] = MagicMock()
        sys.modules["src.tools.read_only"] = MagicMock()
        sys.modules["src.tools.edit_write"] = MagicMock()
        sys.modules["src.tools.compaction_tool"] = MagicMock()

        with patch.dict("sys.modules", {
            "prompt_toolkit": mock_pt,
            "openai": mock_openai,
            "agents": mock_agents,
        }, clear=False):
            if "scripts.cli" in sys.modules:
                del sys.modules["scripts.cli"]
            for mod in list(sys.modules.keys()):
                if mod.startswith("src.runtime"):
                    del sys.modules[mod]
            from scripts.cli import stream_reply

            config = MagicMock()
            config.api_key = "test-key"
            config.model = "gpt-4o"
            config.base_url = "https://api.openai.com/v1"

            session_runtime = MagicMock()
            session_runtime.session = MagicMock()
            session_runtime.context = MagicMock()
            session_runtime.context.workspace_root = Path.cwd()
            session_runtime.context.current_model = "gpt-4o"
            session_runtime.context.main_model = "gpt-4o"
            session_runtime.context.light_model = "gpt-4o"
            session_runtime.context.start_trace_run = MagicMock(return_value="test-run-id")
            session_runtime.update_name_from_user_input = MagicMock()

            with patch("scripts.cli.asyncio.run") as mock_asyncio_run:
                mock_asyncio_run.side_effect = Exception("Connection failed")

                stderr_capture = StringIO()
                with patch("sys.stderr", stderr_capture):
                    stream_reply("test input", config, session_runtime)

                output = stderr_capture.getvalue()
                assert "模型连接失败" in output


class TestRunRepl:
    """Test suite for run_repl function."""

    def test_run_repl_catches_api_connection_error(self):
        """Verify that run_repl properly catches and handles APIConnectionError."""
        mock_pt = _create_mock_prompt_toolkit()
        mock_pt.PromptSession = MagicMock()
        mock_pt.history.FileHistory = MagicMock()
        mock_pt.key_binding.KeyBindings = MagicMock()

        mock_openai = _create_mock_openai()
        mock_openai.APIConnectionError = Exception
        mock_openai.AsyncOpenAI = MagicMock()
        mock_openai.types.responses.ResponseTextDeltaEvent = MagicMock()

        mock_agents = _create_mock_agents()
        mock_agents.Runner = MagicMock()
        mock_agents.RunConfig = MagicMock()
        mock_agents.Agent = MagicMock()
        mock_agents.set_default_openai_api = MagicMock()
        mock_agents.set_default_openai_client = MagicMock()
        mock_agents.set_tracing_disabled = MagicMock()
        mock_agents.TResponseInputItem = MagicMock()
        mock_agents.RunContextWrapper = MagicMock()
        mock_agents.function_tool = MagicMock()
        mock_agents.SQLiteSession = MagicMock()
        mock_agents.items.ToolCallItem = MagicMock()
        mock_agents.items.ToolCallOutputItem = MagicMock()

        mock_tiktoken = MagicMock()
        sys.modules["tiktoken"] = mock_tiktoken

        mock_src_context = MagicMock()
        sys.modules["src.context"] = mock_src_context
        sys.modules["src.context.context_builder"] = MagicMock()
        sys.modules["src.context.compaction"] = MagicMock()
        sys.modules["src.runtime.agent_factory"] = MagicMock()
        sys.modules["src.tools.registry"] = MagicMock()
        sys.modules["src.tools.bash_tool"] = MagicMock()
        sys.modules["src.tools.worktree_tools"] = MagicMock()
        sys.modules["src.tools.todo_write"] = MagicMock()
        sys.modules["src.tools.team_tools"] = MagicMock()
        sys.modules["src.tools.task_tools"] = MagicMock()
        sys.modules["src.tools.skill_tool"] = MagicMock()
        sys.modules["src.tools.read_only"] = MagicMock()
        sys.modules["src.tools.edit_write"] = MagicMock()
        sys.modules["src.tools.compaction_tool"] = MagicMock()

        with patch.dict("sys.modules", {
            "prompt_toolkit": mock_pt,
            "openai": mock_openai,
            "agents": mock_agents,
        }, clear=False):
            if "scripts.cli" in sys.modules:
                del sys.modules["scripts.cli"]
            for mod in list(sys.modules.keys()):
                if mod.startswith("src.runtime"):
                    del sys.modules[mod]
            from scripts.cli import run_repl

            config = MagicMock()
            config.api_key = "test-key"
            config.model = "gpt-4o"
            config.base_url = "https://api.openai.com/v1"

            session_runtime = MagicMock()
            session_runtime.session_name = "test-session"
            session_runtime.session_id = "test-id"
            session_runtime.context = MagicMock()
            session_runtime.context.workspace_root = Path.cwd()
            session_runtime.update_name_from_user_input = MagicMock()

            user_input_generator = iter(["exit"])

            with patch("scripts.cli.build_prompt_session") as mock_session:
                mock_prompt_session = MagicMock()
                mock_prompt_session.prompt = MagicMock(side_effect=user_input_generator)
                mock_session.return_value = mock_prompt_session

                with patch("scripts.cli.build_cli_session_runtime") as mock_build_session:
                    mock_session_runtime = MagicMock()
                    mock_session_runtime.session_name = "test-session"
                    mock_session_runtime.session_id = "test-id"
                    mock_session_runtime.context = MagicMock()
                    mock_session_runtime.context.workspace_root = Path.cwd()
                    mock_session_runtime.update_name_from_user_input = MagicMock()
                    mock_session_runtime.close = MagicMock()
                    mock_build_session.return_value = mock_session_runtime

                    with patch("scripts.cli.stream_reply") as mock_stream_reply:
                        mock_stream_reply.side_effect = Exception("Connection failed")

                        stderr_capture = StringIO()
                        stdout_capture = StringIO()

                        with patch("sys.stderr", stderr_capture), patch("sys.stdout", stdout_capture):
                            return_code = run_repl(config)

                        assert return_code == 0


class TestRuntimeConfigLoading:
    """Test suite for runtime configuration loading."""

    def test_load_runtime_config_missing_api_key_raises_system_exit(self):
        """Verify that missing API key results in SystemExit."""
        with patch.dict("os.environ", {}, clear=True):
            with patch("dotenv.load_dotenv") as mock_load_dotenv:
                mock_load_dotenv.side_effect = lambda *args, **kwargs: None

                import importlib
                import src.runtime.config
                importlib.reload(src.runtime.config)

                from src.runtime.config import load_runtime_config

                with pytest.raises(SystemExit) as exc_info:
                    load_runtime_config()

                assert exc_info.value.code == 1

                importlib.reload(src.runtime.config)

    def test_load_runtime_config_with_valid_env_vars(self):
        """Verify that valid environment variables are properly loaded."""
        with patch.dict("os.environ", {
            "OPENAI_API_KEY": "sk-test-key-123",
            "OPENAI_MODEL": "gpt-4o",
            "OPENAI_BASE_URL": "https://api.test.com/v1",
            "LIGHT_OPENAI_MODEL": "gpt-4o-mini",
        }, clear=True):
            with patch("dotenv.load_dotenv") as mock_load_dotenv:
                mock_load_dotenv.side_effect = lambda *args, **kwargs: None

                import importlib
                import src.runtime.config
                importlib.reload(src.runtime.config)

                from src.runtime.config import load_runtime_config

                config = load_runtime_config()

                assert config.api_key == "sk-test-key-123"
                assert config.model == "gpt-4o"
                assert config.base_url == "https://api.test.com/v1"
                assert config.light_model == "gpt-4o-mini"

                importlib.reload(src.runtime.config)

    def test_load_runtime_config_bearer_prefix_stripped(self):
        """Verify that Bearer prefix is properly stripped from API key."""
        with patch.dict("os.environ", {
            "OPENAI_API_KEY": "Bearer sk-test-key-123",
        }, clear=True):
            with patch("dotenv.load_dotenv") as mock_load_dotenv:
                mock_load_dotenv.side_effect = lambda *args, **kwargs: None

                import importlib
                import src.runtime.config
                importlib.reload(src.runtime.config)

                from src.runtime.config import load_runtime_config

                config = load_runtime_config()
                assert config.api_key == "sk-test-key-123"

                importlib.reload(src.runtime.config)

    def test_load_runtime_config_empty_base_url_becomes_none(self):
        """Verify that empty base URL becomes None."""
        with patch.dict("os.environ", {
            "OPENAI_API_KEY": "sk-test-key-123",
            "OPENAI_BASE_URL": "",
        }, clear=True):
            with patch("dotenv.load_dotenv") as mock_load_dotenv:
                mock_load_dotenv.side_effect = lambda *args, **kwargs: None

                import importlib
                import src.runtime.config
                importlib.reload(src.runtime.config)

                from src.runtime.config import load_runtime_config

                config = load_runtime_config()
                assert config.base_url is None

                importlib.reload(src.runtime.config)


class TestConfigureOpenAIRuntime:
    """Test suite for OpenAI runtime configuration."""

    def test_configure_openai_runtime_sets_client_correctly(self):
        """Verify that configure_openai_runtime properly initializes the OpenAI client."""
        mock_agents = _create_mock_agents()
        mock_agents.Runner = MagicMock()
        mock_agents.RunConfig = MagicMock()
        mock_agents.Agent = MagicMock()
        mock_agents.set_default_openai_api = MagicMock()
        mock_agents.set_default_openai_client = MagicMock()
        mock_agents.set_tracing_disabled = MagicMock()
        mock_agents.TResponseInputItem = MagicMock()
        mock_agents.RunContextWrapper = MagicMock()
        mock_agents.function_tool = MagicMock()
        mock_agents.SQLiteSession = MagicMock()
        mock_agents.items.ToolCallItem = MagicMock()
        mock_agents.items.ToolCallOutputItem = MagicMock()

        mock_openai = _create_mock_openai()
        mock_openai.AsyncOpenAI = MagicMock()
        mock_openai.types.responses.ResponseTextDeltaEvent = MagicMock()

        mock_tiktoken = MagicMock()
        sys.modules["tiktoken"] = mock_tiktoken

        mock_src_context = MagicMock()
        sys.modules["src.context"] = mock_src_context
        sys.modules["src.context.context_builder"] = MagicMock()
        sys.modules["src.context.compaction"] = MagicMock()
        sys.modules["src.runtime.agent_factory"] = MagicMock()

        with patch.dict("sys.modules", {
            "openai": mock_openai,
            "agents": mock_agents,
        }, clear=False):
            if "src.runtime.runner" in sys.modules:
                del sys.modules["src.runtime.runner"]
            for mod in list(sys.modules.keys()):
                if mod.startswith("src.runtime"):
                    del sys.modules[mod]
            from src.runtime.runner import configure_openai_runtime
            from src.runtime.config import RuntimeConfig

            config = RuntimeConfig(
                api_key="sk-test-key-123",
                model="gpt-4o",
                light_model="gpt-4o-mini",
                base_url="https://api.test.com/v1",
            )

            with patch("src.runtime.runner.set_default_openai_client") as mock_set_client:
                with patch("src.runtime.runner.set_default_openai_api"):
                    with patch("src.runtime.runner.set_tracing_disabled"):
                        with patch("src.runtime.runner.AsyncOpenAI") as mock_client_class:
                            configure_openai_runtime(config)

                            mock_client_class.assert_called_once_with(
                                base_url="https://api.test.com/v1",
                                api_key="sk-test-key-123",
                            )
                            mock_set_client.assert_called_once()
