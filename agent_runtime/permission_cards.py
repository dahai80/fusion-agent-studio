# N9 (#348): Permission Cards — four approval card classes
# Folder / Code / Network / Remote. Pure category mapping, no LLM calls.
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

CARD_FOLDER = "folder"
CARD_CODE = "code"
CARD_NETWORK = "network"
CARD_REMOTE = "remote"

CARD_TYPES = (CARD_FOLDER, CARD_CODE, CARD_NETWORK, CARD_REMOTE)

CATEGORY_TO_CARD: dict[str, str] = {
    "file_read": CARD_FOLDER,
    "file_write": CARD_FOLDER,
    "doc_retrieval": CARD_FOLDER,
    "knowledge_search": CARD_FOLDER,
    "code_analysis": CARD_CODE,
    "code_edit": CARD_CODE,
    "shell_exec": CARD_CODE,
    "git_push": CARD_CODE,
    "database_write": CARD_CODE,
    "network_access": CARD_NETWORK,
    "tool_call": CARD_REMOTE,
    "llm_call": CARD_REMOTE,
}

DEFAULT_CARD = CARD_CODE


def card_type_for_category(category: str) -> str:
    card = CATEGORY_TO_CARD.get(category or "")
    if card is None:
        logger.info("permission card: unknown category '%s', defaulting to %s", category, DEFAULT_CARD)
        return DEFAULT_CARD
    return card


def build_card(action: dict[str, Any]) -> dict[str, Any]:
    category = str(action.get("category", "") or "")
    card_type = card_type_for_category(category)
    content = action.get("content", "") or ""
    record: dict[str, Any] = {
        "action_id": action.get("action_id", ""),
        "card_type": card_type,
        "category": category,
        "level": action.get("level", ""),
        "status": action.get("status", "pending"),
        "content": content,
        "requires_diff": bool(action.get("diff")),
    }
    if action.get("diff"):
        record["diff"] = action["diff"]
    if action.get("original") is not None:
        record["original"] = action["original"]
    if action.get("proposed") is not None:
        record["proposed"] = action["proposed"]
    return record
