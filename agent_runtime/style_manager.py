from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STYLES_DIR = "styles"
STYLES_INDEX = "styles_index.json"

BUILTIN_STYLES = [
    {
        "id": "formal-report",
        "name": "正式商业报告",
        "suffix": "请以正式商业报告风格输出，使用结构化标题、数据引用和结论摘要。语言严谨专业。",
        "output_format": "markdown",
    },
    {
        "id": "technical-doc",
        "name": "技术文档",
        "suffix": "请以技术文档风格输出，包含代码示例、参数说明和架构图描述。语言精确简洁。",
        "output_format": "markdown",
    },
    {
        "id": "creative-writing",
        "name": "创意写作",
        "suffix": "请以创意写作风格输出，注重表达力、叙事节奏和语言美感。自由发挥。",
        "output_format": "plain",
    },
    {
        "id": "json-structured",
        "name": "JSON结构化输出",
        "suffix": "请以JSON格式输出，确保结构清晰、字段命名规范、值类型一致。",
        "output_format": "json",
    },
    {
        "id": "concise-summary",
        "name": "精简摘要",
        "suffix": "请以精简摘要风格输出，控制在200字以内，突出核心结论和关键数据。",
        "output_format": "plain",
    },
]


@dataclass
class StyleConfig:
    id: str = ""
    name: str = ""
    suffix: str = ""
    output_format: str = "markdown"
    created_at: float = 0.0
    is_builtin: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "suffix": self.suffix,
            "output_format": self.output_format,
            "created_at": self.created_at,
            "is_builtin": self.is_builtin,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StyleConfig:
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            suffix=data.get("suffix", ""),
            output_format=data.get("output_format", "markdown"),
            created_at=data.get("created_at", 0.0),
            is_builtin=data.get("is_builtin", False),
        )


class StyleManager:
    def __init__(self, base_path: str | Path):
        self.base_path = Path(base_path)
        self._styles: dict[str, StyleConfig] = {}
        self._init_builtins()
        self._load_index()

    @property
    def index_path(self) -> Path:
        return self.base_path / STYLES_INDEX

    def _init_builtins(self) -> None:
        for s in BUILTIN_STYLES:
            cfg = StyleConfig(
                id=s["id"],
                name=s["name"],
                suffix=s["suffix"],
                output_format=s["output_format"],
                created_at=0.0,
                is_builtin=True,
            )
            self._styles[cfg.id] = cfg

    def _load_index(self) -> None:
        if self.index_path.exists():
            try:
                with open(self.index_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for entry in data:
                    cfg = StyleConfig.from_dict(entry)
                    if not cfg.is_builtin:
                        self._styles[cfg.id] = cfg
                logger.info("Loaded %d custom styles", len(data))
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("Failed to load styles index: %s", exc)

    def _persist_index(self) -> None:
        self.base_path.mkdir(parents=True, exist_ok=True)
        custom = [s.to_dict() for s in self._styles.values() if not s.is_builtin]
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(custom, f, indent=4, ensure_ascii=False)
        logger.debug("Persisted styles index: %d custom entries", len(custom))

    def list_styles(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self._styles.values()]

    def get(self, style_id: str) -> dict[str, Any] | None:
        cfg = self._styles.get(style_id)
        if cfg is None:
            return None
        return cfg.to_dict()

    def create(self, name: str, suffix: str, output_format: str = "markdown") -> dict[str, Any]:
        import uuid
        style_id = f"custom-{uuid.uuid4().hex[:8]}"
        now = time.time()
        cfg = StyleConfig(
            id=style_id,
            name=name,
            suffix=suffix,
            output_format=output_format,
            created_at=now,
            is_builtin=False,
        )
        self._styles[style_id] = cfg
        self._persist_index()
        logger.info("style.create: id=%s name=%s", style_id, name)
        return {"style_id": style_id, "style": cfg.to_dict()}

    def apply(self, system_prompt: str, style_id: str) -> dict[str, Any]:
        cfg = self._styles.get(style_id)
        if cfg is None:
            return {"status": "error", "message": f"Style not found: {style_id}"}
        augmented = f"{system_prompt}\n\n{cfg.suffix}"
        logger.info("style.apply: style=%s format=%s", style_id, cfg.output_format)
        return {"system_prompt": augmented, "style": style_id, "output_format": cfg.output_format}
