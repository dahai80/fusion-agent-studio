"""Internationalization (i18n) — multi-language support for UI strings."""

from __future__ import annotations

import json
from pathlib import Path

# Default English locale
EN_LOCALE: dict[str, str] = {
    # Canvas
    "canvas.title": "Agent Canvas",
    "canvas.new": "+ New",
    "canvas.save": "💾 Save",
    "canvas.load": "📂 Load",
    "canvas.export": "📤 Export",
    "canvas.run": "▶ Run",
    "canvas.node_types": "NODE TYPES",
    "canvas.properties": "PROPERTIES",
    "canvas.select_node": "Select a node to edit its properties",
    "canvas.no_graph": "Drag nodes from the palette to start building your agent graph",
    "canvas.clear_confirm": "Clear current graph?",
    # Node types
    "node.start": "Start",
    "node.llm": "LLM Think",
    "node.tool": "Tool Call",
    "node.condition": "Condition",
    "node.loop": "Loop",
    "node.end": "End",
    "node.error_handler": "Error Handler",
    # Properties
    "prop.label": "Label",
    "prop.type": "Type",
    "prop.model": "Model",
    "prop.temperature": "Temperature",
    "prop.max_tokens": "Max Tokens",
    "prop.system_prompt": "System Prompt",
    "prop.tool_name": "Tool Name",
    "prop.condition": "Condition",
    "prop.max_iterations": "Max Iterations",
    "prop.max_retries": "Max Retries",
    "prop.retry_delay": "Retry Delay (s)",
    # Actions
    "action.saved": "Graph saved",
    "action.updated": "Graph updated",
    "action.deleted": "Graph deleted",
    "action.failed": "Operation failed",
    "action.confirm": "Confirm",
    "action.cancel": "Cancel",
    # Agent
    "agent.running": "Running...",
    "agent.completed": "Completed",
    "agent.failed": "Failed",
    "agent.paused": "Paused",
    "agent.thinking": "Thinking...",
    "agent.executing_tool": "Executing tool...",
    # Errors
    "error.no_graph": "No saved graphs",
    "error.graph_empty": "Graph is empty",
    "error.graph_not_found": "Graph not found",
    "error.save_failed": "Failed to save graph",
    "error.load_failed": "Failed to load graph",
    "error.run_failed": "Failed to run graph",
}

# Chinese locale
ZH_LOCALE: dict[str, str] = {
    "canvas.title": "Agent 画布",
    "canvas.new": "+ 新建",
    "canvas.save": "💾 保存",
    "canvas.load": "📂 加载",
    "canvas.export": "📤 导出",
    "canvas.run": "▶ 运行",
    "canvas.node_types": "节点类型",
    "canvas.properties": "属性",
    "canvas.select_node": "选择一个节点以编辑其属性",
    "canvas.no_graph": "从调色板拖拽节点开始构建你的 Agent 流程图",
    "canvas.clear_confirm": "清空当前流程图？",
    "node.start": "开始",
    "node.llm": "LLM 思考",
    "node.tool": "工具调用",
    "node.condition": "条件判断",
    "node.loop": "循环",
    "node.end": "结束",
    "node.error_handler": "错误处理",
    "prop.label": "标签",
    "prop.type": "类型",
    "prop.model": "模型",
    "prop.temperature": "温度",
    "prop.max_tokens": "最大 Token",
    "prop.system_prompt": "系统提示词",
    "prop.tool_name": "工具名称",
    "prop.condition": "条件表达式",
    "prop.max_iterations": "最大循环次数",
    "prop.max_retries": "最大重试次数",
    "prop.retry_delay": "重试延迟 (秒)",
    "action.saved": "流程图已保存",
    "action.updated": "流程图已更新",
    "action.deleted": "流程图已删除",
    "action.failed": "操作失败",
    "action.confirm": "确认",
    "action.cancel": "取消",
    "agent.running": "运行中...",
    "agent.completed": "已完成",
    "agent.failed": "失败",
    "agent.paused": "已暂停",
    "agent.thinking": "思考中...",
    "agent.executing_tool": "执行工具...",
    "error.no_graph": "没有保存的流程图",
    "error.graph_empty": "流程图为空",
    "error.graph_not_found": "流程图未找到",
    "error.save_failed": "保存流程图失败",
    "error.load_failed": "加载流程图失败",
    "error.run_failed": "运行流程图失败",
}


class I18n:
    """Internationalization manager.

    Usage:
        i18n = I18n("zh")
        label = i18n.t("canvas.title")  # "Agent 画布"
    """

    _locales: dict[str, dict[str, str]] = {
        "en": EN_LOCALE,
        "zh": ZH_LOCALE,
    }

    def __init__(self, language: str = "en"):
        self.language = language if language in self._locales else "en"

    def t(self, key: str, default: str = "") -> str:
        """Translate a key to the current language."""
        locale = self._locales.get(self.language, EN_LOCALE)
        return locale.get(key, default or key)

    def set_language(self, language: str) -> None:
        if language in self._locales:
            self.language = language

    @classmethod
    def register_locale(cls, language: str, translations: dict[str, str]) -> None:
        cls._locales[language] = translations

    @classmethod
    def available_languages(cls) -> list[str]:
        return list(cls._locales.keys())

    @classmethod
    def load_from_file(cls, language: str, filepath: str | Path) -> None:
        path = Path(filepath).expanduser().resolve()
        if path.exists():
            translations = json.loads(path.read_text(encoding="utf-8"))
            cls.register_locale(language, translations)
