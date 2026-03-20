"""Tests for DJcode CLI and core modules."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from djcode import __version__
from djcode.cli import main
from djcode.config import DEFAULT_CONFIG, load_config, save_config
from djcode.memory.embedder import cosine_similarity
from djcode.memory.manager import MemoryManager
from djcode.prompt import build_system_prompt
from djcode.provider import Message, ProviderConfig


# -- CLI Tests --


class TestCLI:
    """Test the Click CLI interface."""

    def test_version(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "DJcode" in result.output
        assert "DarshJ.AI" in result.output

    def test_config_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--config"])
        assert result.exit_code == 0
        assert "Configuration" in result.output


# -- Config Tests --


class TestConfig:
    """Test configuration management."""

    def test_default_config(self) -> None:
        cfg = DEFAULT_CONFIG
        assert cfg["provider"] == "ollama"
        assert cfg["model"] == "qwen3:32b"
        assert cfg["telemetry"] is False

    def test_load_save_roundtrip(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.json"
        test_config = {"provider": "mlx", "model": "test-model"}

        with open(config_file, "w") as f:
            json.dump(test_config, f)

        with open(config_file) as f:
            loaded = json.load(f)

        assert loaded["provider"] == "mlx"
        assert loaded["model"] == "test-model"


# -- Provider Tests --


class TestProviderConfig:
    """Test provider configuration."""

    def test_provider_config_creation(self) -> None:
        cfg = ProviderConfig(
            name="ollama",
            base_url="http://localhost:11434",
            model="qwen3:32b",
        )
        assert cfg.name == "ollama"
        assert cfg.model == "qwen3:32b"
        assert cfg.temperature == 0.7

    def test_message_creation(self) -> None:
        msg = Message(role="user", content="hello")
        assert msg.role == "user"
        assert msg.content == "hello"
        assert msg.tool_calls == []


# -- System Prompt Tests --


class TestPrompt:
    """Test system prompt generation."""

    def test_basic_prompt(self) -> None:
        prompt = build_system_prompt()
        assert "DJcode" in prompt
        assert "DarshJ.AI" in prompt
        assert "local-first" in prompt.lower() or "Local-first" in prompt

    def test_bypass_rlhf_prompt(self) -> None:
        prompt = build_system_prompt(bypass_rlhf=True)
        assert "Unrestricted Mode" in prompt

    def test_no_external_mentions(self) -> None:
        prompt = build_system_prompt()
        for forbidden in ["Claude", "Anthropic", "OpenAI", "Gemini", "OpenCode"]:
            assert forbidden not in prompt, f"Found forbidden term: {forbidden}"


# -- Memory Tests --


class TestMemory:
    """Test the memory manager."""

    def test_session_memory(self) -> None:
        mgr = MemoryManager()
        mgr.clear_session()
        mgr.add_session_message("user", "hello")
        mgr.add_session_message("assistant", "hi there")

        messages = mgr.get_session_messages()
        assert len(messages) == 2
        assert messages[0]["role"] == "user"

    def test_persistent_memory(self, tmp_path: Path) -> None:
        # Patch MEMORY_DIR to use tmp
        import djcode.memory.manager as mm
        import djcode.config as cfg_mod

        original_dir = mm.MEMORY_DIR
        original_facts = mm.FACTS_FILE
        mm.MEMORY_DIR = tmp_path
        mm.FACTS_FILE = tmp_path / "facts.json"
        cfg_mod.MEMORY_DIR = tmp_path

        try:
            mgr = MemoryManager()
            mgr.remember("test_key", "test_value", tags=["test"])

            value = mgr.recall("test_key")
            assert value == "test_value"

            facts = mgr.list_facts()
            assert "test_key" in facts

            mgr.forget("test_key")
            assert mgr.recall("test_key") is None
        finally:
            mm.MEMORY_DIR = original_dir
            mm.FACTS_FILE = original_facts
            cfg_mod.MEMORY_DIR = original_dir

    def test_cosine_similarity(self) -> None:
        a = [1.0, 0.0, 0.0]
        b = [1.0, 0.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(1.0)

        c = [0.0, 1.0, 0.0]
        assert cosine_similarity(a, c) == pytest.approx(0.0)

    def test_memory_stats(self) -> None:
        mgr = MemoryManager()
        mgr.clear_session()
        stats = mgr.stats
        assert "session_messages" in stats
        assert "persistent_facts" in stats


# -- Tool Tests --


class TestTools:
    """Test tool execution."""

    def test_file_read(self) -> None:
        from djcode.tools.file_read import execute_file_read

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("line1\nline2\nline3\n")
            f.flush()
            path = f.name

        try:
            result = asyncio.run(execute_file_read(path))
            assert "line1" in result
            assert "line2" in result
        finally:
            os.unlink(path)

    def test_file_write(self) -> None:
        from djcode.tools.file_write import execute_file_write

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.txt")
            result = asyncio.run(execute_file_write(path, "hello world"))
            assert "Wrote" in result
            assert Path(path).read_text() == "hello world"

    def test_file_edit(self) -> None:
        from djcode.tools.file_edit import execute_file_edit

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("hello world")
            f.flush()
            path = f.name

        try:
            result = asyncio.run(execute_file_edit(path, "hello", "goodbye"))
            assert "replaced" in result.lower() or "Edited" in result
            assert Path(path).read_text() == "goodbye world"
        finally:
            os.unlink(path)

    def test_bash(self) -> None:
        from djcode.tools.bash import execute_bash

        result = asyncio.run(execute_bash("echo hello"))
        assert "hello" in result

    def test_glob(self) -> None:
        from djcode.tools.glob import execute_glob

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.py").write_text("x")
            Path(tmp, "b.py").write_text("y")
            result = asyncio.run(execute_glob("*.py", tmp))
            assert "a.py" in result
            assert "b.py" in result

    def test_git_status(self) -> None:
        from djcode.tools.git import execute_git

        result = asyncio.run(execute_git("--version"))
        assert "git version" in result.lower() or "git" in result.lower()

    def test_git_dangerous_blocked(self) -> None:
        from djcode.tools.git import execute_git

        result = asyncio.run(execute_git("reset --hard HEAD"))
        assert "destructive" in result.lower() or "Warning" in result
