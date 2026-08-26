"""Plugin system — dynamically load user-defined tools from external Python files."""
from __future__ import annotations

import importlib.util
import logging
import re
from pathlib import Path
from typing import Any

from tools.base import BaseTool
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# 审计 P1-24/E-6: plugin name 仅允许安全字符 (字母数字下划线), 挡路径穿越
# (name="../../../etc/passwd" 逃出 plugin_dir). 命名规范同 Python 标识符.
_SAFE_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PluginManager:
    """Manages user-defined plugin tools loaded from external Python files.

    Plugins are Python files that define subclasses of BaseTool.
    The plugin manager scans a directory, imports each file, and
    registers any BaseTool subclass found.
    """

    def __init__(self, registry: ToolRegistry, plugin_dir: str | Path = ""):
        self.registry = registry
        if not plugin_dir:
            plugin_dir = Path.home() / ".fusion-agent-studio" / "plugins"
        self.plugin_dir = Path(plugin_dir)
        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        self._loaded: dict[str, str] = {}  # plugin_name -> file_path
        self._failed: dict[str, str] = {}  # plugin_name -> error (显式化加载失败, 见 issue #164)

    def discover(self) -> list[dict[str, Any]]:
        """Scan the plugin directory and return metadata about available plugins."""
        plugins = []
        if not self.plugin_dir.exists():
            return plugins
        for f in sorted(self.plugin_dir.iterdir()):
            if f.suffix == ".py" and not f.name.startswith("_"):
                plugins.append({
                    "name": f.stem,
                    "path": str(f),
                    "loaded": f.stem in self._loaded,
                })
        return plugins

    def load_plugin(self, name: str) -> BaseTool | None:
        """Load a single plugin by name (without .py suffix)."""
        # 审计 P1-24/E-6: name 安全校验 — 挡路径穿越 + 非法字符 (防 ../../etc/passwd).
        if not _SAFE_TOOL_NAME_RE.match(name):
            logger.warning("Plugin name rejected (unsafe chars): %r", name)
            self._failed[name] = "unsafe plugin name (path traversal blocked)"
            return None
        plugin_path = self.plugin_dir / f"{name}.py"
        # 审计 P1-24/E-6: resolve 后确认仍在 plugin_dir 内 (双保险挡符号链接/穿越).
        try:
            resolved = plugin_path.resolve()
            if not str(resolved).startswith(str(self.plugin_dir.resolve())):
                logger.warning("Plugin path escapes plugin_dir: %s", resolved)
                self._failed[name] = "plugin path escapes plugin_dir"
                return None
        except OSError as e:
            logger.warning("Plugin path resolve failed: %s: %s", plugin_path, e)
            return None
        if not plugin_path.exists():
            logger.warning("Plugin not found: %s", plugin_path)
            return None
        try:
            spec = importlib.util.spec_from_file_location(name, plugin_path)
            if spec is None or spec.loader is None:
                return None
            mod = importlib.util.module_from_spec(spec)
            logger.info("loading plugin (exec_module) name=%s path=%s", name, plugin_path)
            spec.loader.exec_module(mod)
            # Find BaseTool subclasses in the module
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if isinstance(attr, type) and issubclass(attr, BaseTool) and attr is not BaseTool:
                    tool = attr()
                    self.registry.register(tool)
                    self._loaded[name] = str(plugin_path)
                    logger.info("Loaded plugin: %s -> %s", name, tool.name)
                    return tool
            logger.warning("No BaseTool subclass found in plugin: %s", name)
            return None
        except Exception as e:
            logger.error("Failed to load plugin %s: %s", name, e)
            self._failed[name] = str(e)
            return None

    def load_all(self) -> list[BaseTool]:
        """Load all plugins from the plugin directory."""
        # 审计 A-3: 自动扫描加载未签名 .py 文件 exec_module = 进程内全权限.
        # secure-by-default: daemon 启动时自动加载需显式 env FUSION_PLUGINS_ENABLE=1.
        # 单个 load_plugin(name) 仍可经 RPC 显式调用 (用户主动指定, 非自动执行).
        import os
        if os.environ.get("FUSION_PLUGINS_ENABLE", "").strip().lower() not in ("1", "true", "yes"):
            logger.info(
                "plugin auto-load disabled (secure-by-default); "
                "set FUSION_PLUGINS_ENABLE=1 to load plugins from %s",
                self.plugin_dir,
            )
            return []
        tools = []
        for plugin in self.discover():
            tool = self.load_plugin(plugin["name"])
            if tool:
                tools.append(tool)
        if self._failed:
            logger.warning(
                "plugin load failures (%d): %s — daemon may be missing tools, "
                "check venv deps (pip install -e .[plugins-extra])",
                len(self._failed),
                list(self._failed.keys()),
            )
        return tools

    def unload(self, name: str) -> None:
        """Unload a plugin by name."""
        if name in self._loaded:
            # Find the tool name from the registry
            for tool_name in list(self.registry._tools.keys()):
                # We can't easily map back, so just unload all from this plugin
                pass
            self._loaded.pop(name, None)
            logger.info("Unloaded plugin: %s", name)

    def create_plugin_template(self, name: str, description: str = "") -> Path:
        """Create a boilerplate plugin file for users to customize."""
        plugin_path = self.plugin_dir / f"{name}.py"
        content = f'''"""Custom plugin: {name}"""
from tools.base import BaseTool


class {name.capitalize()}Tool(BaseTool):
    """{description or f"A custom tool named {name}"}"""

    name = "{name}"
    description = "{description or f'A custom tool named {name}'}"
    parameters = {{
        "input": {{
            "type": "string",
            "description": "Input parameter",
        }},
    }}

    async def execute(self, **kwargs) -> str:
        # Your tool logic here
        input_val = kwargs.get("input", "")
        return f"Executed {{self.name}} with input: {{input_val}}"
'''
        plugin_path.write_text(content)
        logger.info("Created plugin template: %s", plugin_path)
        return plugin_path

    @property
    def loaded_count(self) -> int:
        return len(self._loaded)

    @property
    def failed_plugins(self) -> dict[str, str]:
        # 暴露加载失败的 plugin (name->error), 供 daemon.status/监控显式化, 见 issue #164.
        return dict(self._failed)