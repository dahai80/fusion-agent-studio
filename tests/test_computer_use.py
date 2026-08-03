"""Tests for Computer Use tools (screen_capture, mouse, keyboard, clipboard)."""

from __future__ import annotations

import pytest

from tools.computer_use_tools import (
    ScreenCaptureTool,
    MouseTool,
    KeyboardTool,
    ClipboardTool,
    _PLATFORM_OK,
    _QUARTZ_OK,
    _KEY_MAP,
)
from tools import create_default_registry


@pytest.mark.skipif(not _QUARTZ_OK, reason="Quartz not available")
class TestScreenCaptureTool:
    def test_schema(self):
        tool = ScreenCaptureTool()
        schema = tool.openai_schema()
        assert schema["function"]["name"] == "screen_capture"
        assert "x" in schema["function"]["parameters"]["properties"]

    @pytest.mark.asyncio
    async def test_capture_full_screen(self):
        tool = ScreenCaptureTool()
        result = await tool.execute()
        assert not result.startswith("Error:")
        assert "base64" in result or result.endswith(".png")

    @pytest.mark.asyncio
    async def test_capture_region(self):
        tool = ScreenCaptureTool()
        result = await tool.execute(x=0, y=0, width=100, height=100)
        assert not result.startswith("Error:")


@pytest.mark.skipif(not _QUARTZ_OK, reason="Quartz not available")
class TestMouseTool:
    def test_schema(self):
        tool = MouseTool()
        schema = tool.openai_schema()
        assert schema["function"]["name"] == "mouse"

    @pytest.mark.asyncio
    async def test_move(self):
        tool = MouseTool()
        result = await tool.execute(action="move", x=100, y=100)
        assert "Moved" in result or result.startswith("Error:")

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        tool = MouseTool()
        result = await tool.execute(action="fly", x=0, y=0)
        assert "Unknown" in result or "Error:" in result


@pytest.mark.skipif(not _QUARTZ_OK, reason="Quartz not available")
class TestKeyboardTool:
    def test_schema(self):
        tool = KeyboardTool()
        schema = tool.openai_schema()
        assert schema["function"]["name"] == "keyboard"

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        tool = KeyboardTool()
        result = await tool.execute(action="fly")
        assert "Unknown" in result or "Error:" in result

    @pytest.mark.asyncio
    async def test_key_missing_param(self):
        tool = KeyboardTool()
        result = await tool.execute(action="key")
        assert "required" in result.lower() or "Error:" in result

    @pytest.mark.asyncio
    async def test_unknown_key(self):
        tool = KeyboardTool()
        result = await tool.execute(action="key", key="nonexistent_key_xyz")
        assert "Unknown key" in result or "Error:" in result

    def test_key_map_has_basic_keys(self):
        assert "return" in _KEY_MAP
        assert "a" in _KEY_MAP
        assert "cmd" in _KEY_MAP
        assert "f1" in _KEY_MAP


class TestClipboardTool:
    @pytest.mark.asyncio
    async def test_schema(self):
        tool = ClipboardTool()
        schema = tool.openai_schema()
        assert schema["function"]["name"] == "clipboard"

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        tool = ClipboardTool()
        result = await tool.execute(action="fly")
        assert "Unknown" in result

    @pytest.mark.skipif(not _PLATFORM_OK, reason="macOS only")
    @pytest.mark.asyncio
    async def test_write_and_read(self):
        tool = ClipboardTool()
        write_result = await tool.execute(action="write", text="fusion-test-clipboard")
        assert "Copied" in write_result or write_result.startswith("Error:")
        if not write_result.startswith("Error:"):
            read_result = await tool.execute(action="read")
            assert "fusion-test-clipboard" in read_result


class TestRegistryIntegration:
    def test_computer_use_tools_in_registry(self):
        registry = create_default_registry()
        assert registry.has("screen_capture")
        assert registry.has("mouse")
        assert registry.has("keyboard")
        assert registry.has("clipboard")
        assert registry.count == 28

    def test_tool_schemas_valid(self):
        registry = create_default_registry()
        for name in ("screen_capture", "mouse", "keyboard", "clipboard"):
            tool = registry.get(name)
            schema = tool.openai_schema()
            assert schema["type"] == "function"
            assert schema["function"]["name"] == name
            assert "parameters" in schema["function"]
