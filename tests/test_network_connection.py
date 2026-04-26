"""Tests for network connection handling.

This module tests network connection scenarios, including successful connections,
connection errors, SSL issues, and network timeouts.
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
    """Create a mock package with proper __path__ for submodule imports."""
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


def _create_mock_agents():
    """Create a mock for the agents package."""
    mock_agents = ModuleType("agents")
    mock_agents.__path__ = []
    sys.modules["agents"] = mock_agents

    mock_items = ModuleType("agents.items")
    mock_items.__path__ = []
    mock_agents.items = mock_items
    sys.modules["agents.items"] = mock_items

    return mock_agents


def _create_mock_openai():
    """Create a mock for the openai package."""
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

    # Mock ResponseTextDeltaEvent
    mock_responses.ResponseTextDeltaEvent = MagicMock()

    return mock_openai


class TestNetworkConnection:
    """Test suite for network connection handling."""

    def test_successful_connection(self):
        """Test that successful connection works properly."""
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
        mock_client = MagicMock()
        mock_openai.AsyncOpenAI = MagicMock(return_value=mock_client)
        mock_openai.APIConnectionError = Exception

        sys.modules["tiktoken"] = MagicMock()
        sys.modules["src.context"] = MagicMock()
        sys.modules["src.context.context_builder"] = MagicMock()
        sys.modules["src.context.compaction"] = MagicMock()
        sys.modules["src.runtime.agent_factory"] = MagicMock()
        sys.modules["src.tools.registry"] = MagicMock()
        sys.modules["src.tools.bash_tool"] = MagicMock()

        with patch.dict("sys.modules", {
            "agents": mock_agents,
            "openai": mock_openai,
        }, clear=False):
            for mod in list(sys.modules.keys()):
                if mod.startswith("src.runtime"):
                    del sys.modules[mod]

            from src.runtime.config import RuntimeConfig
            from src.runtime.runner import configure_openai_runtime

            config = RuntimeConfig(
                api_key="sk-test-key",
                model="gpt-4o",
                light_model="gpt-4o-mini",
                base_url="https://api.openai.com/v1"
            )

            configure_openai_runtime(config)

    def test_connection_error_handling(self):
        """Test that connection errors are properly handled."""
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
        mock_openai.AsyncOpenAI = MagicMock(side_effect=Exception("Connection failed"))
        mock_openai.APIConnectionError = Exception

        sys.modules["tiktoken"] = MagicMock()
        sys.modules["src.context"] = MagicMock()
        sys.modules["src.context.context_builder"] = MagicMock()
        sys.modules["src.context.compaction"] = MagicMock()
        sys.modules["src.runtime.agent_factory"] = MagicMock()
        sys.modules["src.tools.registry"] = MagicMock()
        sys.modules["src.tools.bash_tool"] = MagicMock()

        with patch.dict("sys.modules", {
            "agents": mock_agents,
            "openai": mock_openai,
        }, clear=False):
            for mod in list(sys.modules.keys()):
                if mod.startswith("src.runtime"):
                    del sys.modules[mod]

            from src.runtime.config import RuntimeConfig
            from src.runtime.runner import configure_openai_runtime

            config = RuntimeConfig(
                api_key="sk-test-key",
                model="gpt-4o",
                light_model="gpt-4o-mini",
                base_url="https://api.openai.com/v1"
            )

            with pytest.raises(RuntimeError):
                configure_openai_runtime(config)

    def test_ssl_error_handling(self):
        """Test that SSL errors are properly handled."""
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
        mock_openai.AsyncOpenAI = MagicMock(side_effect=Exception("SSL certificate verification failed"))
        mock_openai.APIConnectionError = Exception

        sys.modules["tiktoken"] = MagicMock()
        sys.modules["src.context"] = MagicMock()
        sys.modules["src.context.context_builder"] = MagicMock()
        sys.modules["src.context.compaction"] = MagicMock()
        sys.modules["src.runtime.agent_factory"] = MagicMock()
        sys.modules["src.tools.registry"] = MagicMock()
        sys.modules["src.tools.bash_tool"] = MagicMock()

        with patch.dict("sys.modules", {
            "agents": mock_agents,
            "openai": mock_openai,
        }, clear=False):
            for mod in list(sys.modules.keys()):
                if mod.startswith("src.runtime"):
                    del sys.modules[mod]

            from src.runtime.config import RuntimeConfig
            from src.runtime.runner import configure_openai_runtime

            config = RuntimeConfig(
                api_key="sk-test-key",
                model="gpt-4o",
                light_model="gpt-4o-mini",
                base_url="https://api.openai.com/v1"
            )

            with pytest.raises(RuntimeError) as exc_info:
                configure_openai_runtime(config)
            
            assert "SSL error detected" in str(exc_info.value)

    def test_timeout_error_handling(self):
        """Test that timeout errors are properly handled."""
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
        mock_openai.AsyncOpenAI = MagicMock(side_effect=Exception("Request timed out"))
        mock_openai.APIConnectionError = Exception

        sys.modules["tiktoken"] = MagicMock()
        sys.modules["src.context"] = MagicMock()
        sys.modules["src.context.context_builder"] = MagicMock()
        sys.modules["src.context.compaction"] = MagicMock()
        sys.modules["src.runtime.agent_factory"] = MagicMock()
        sys.modules["src.tools.registry"] = MagicMock()
        sys.modules["src.tools.bash_tool"] = MagicMock()

        with patch.dict("sys.modules", {
            "agents": mock_agents,
            "openai": mock_openai,
        }, clear=False):
            for mod in list(sys.modules.keys()):
                if mod.startswith("src.runtime"):
                    del sys.modules[mod]

            from src.runtime.config import RuntimeConfig
            from src.runtime.runner import configure_openai_runtime

            config = RuntimeConfig(
                api_key="sk-test-key",
                model="gpt-4o",
                light_model="gpt-4o-mini",
                base_url="https://api.openai.com/v1"
            )

            with pytest.raises(RuntimeError):
                configure_openai_runtime(config)

    def test_invalid_base_url(self):
        """Test that invalid base URL is handled properly."""
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
        mock_openai.AsyncOpenAI = MagicMock(side_effect=Exception("Invalid base URL"))
        mock_openai.APIConnectionError = Exception

        sys.modules["tiktoken"] = MagicMock()
        sys.modules["src.context"] = MagicMock()
        sys.modules["src.context.context_builder"] = MagicMock()
        sys.modules["src.context.compaction"] = MagicMock()
        sys.modules["src.runtime.agent_factory"] = MagicMock()
        sys.modules["src.tools.registry"] = MagicMock()
        sys.modules["src.tools.bash_tool"] = MagicMock()

        with patch.dict("sys.modules", {
            "agents": mock_agents,
            "openai": mock_openai,
        }, clear=False):
            for mod in list(sys.modules.keys()):
                if mod.startswith("src.runtime"):
                    del sys.modules[mod]

            from src.runtime.config import RuntimeConfig
            from src.runtime.runner import configure_openai_runtime

            config = RuntimeConfig(
                api_key="sk-test-key",
                model="gpt-4o",
                light_model="gpt-4o-mini",
                base_url="invalid-url"
            )

            with pytest.raises(RuntimeError) as exc_info:
                configure_openai_runtime(config)
            
            assert "Invalid base URL" in str(exc_info.value)
            assert "Using base URL: invalid-url" in str(exc_info.value)

    def test_empty_api_key(self):
        """Test that empty API key raises SystemExit."""
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=True):
            with patch("dotenv.load_dotenv") as mock_load_dotenv:
                mock_load_dotenv.side_effect = lambda *args, **kwargs: None

                import importlib
                import src.runtime.config
                importlib.reload(src.runtime.config)

                from src.runtime.config import load_runtime_config

                with pytest.raises(SystemExit) as exc_info:
                    load_runtime_config()

                assert exc_info.value.code == 1

    def test_cli_connection_error_handling(self):
        """Test that CLI properly handles connection errors."""
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
        mock_openai.AsyncOpenAI = MagicMock(side_effect=Exception("Connection failed"))
        mock_openai.APIConnectionError = Exception

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

        mock_pt.PromptSession = MagicMock()
        mock_pt.history.FileHistory = MagicMock()
        mock_pt.key_binding.KeyBindings = MagicMock()

        sys.modules["tiktoken"] = MagicMock()
        sys.modules["src.context"] = MagicMock()
        sys.modules["src.context.context_builder"] = MagicMock()
        sys.modules["src.context.compaction"] = MagicMock()
        sys.modules["src.runtime.agent_factory"] = MagicMock()
        sys.modules["src.runtime.session"] = MagicMock()
        sys.modules["src.tools.registry"] = MagicMock()
        sys.modules["src.tools.bash_tool"] = MagicMock()

        with patch.dict("sys.modules", {
            "agents": mock_agents,
            "openai": mock_openai,
            "prompt_toolkit": mock_pt,
        }, clear=False):
            for mod in list(sys.modules.keys()):
                if mod.startswith("src.runtime"):
                    del sys.modules[mod]

            from src.runtime.config import RuntimeConfig
            from scripts.cli import run_repl

            config = RuntimeConfig(
                api_key="sk-test-key",
                model="gpt-4o",
                light_model="gpt-4o-mini",
                base_url="https://api.openai.com/v1"
            )

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
                        mock_stream_reply.side_effect = RuntimeError("Failed to configure OpenAI client: Connection failed")

                        stderr_capture = StringIO()
                        stdout_capture = StringIO()

                        with patch("sys.stderr", stderr_capture), patch("sys.stdout", stdout_capture):
                            return_code = run_repl(config)

                        assert return_code == 0