from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Agent:
    name: str = ""
    agent_id: str = ""
    graph_id: str = ""
    system_prompt: str = ""
    model: str = ""
    skills: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.agent_id:
            self.agent_id = f"agent_{uuid.uuid4().hex[:8]}"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "agent_id": self.agent_id,
            "graph_id": self.graph_id,
            "system_prompt": self.system_prompt,
            "model": self.model,
            "skills": self.skills,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Agent:
        return cls(
            name=data.get("name", ""),
            agent_id=data.get("agent_id", ""),
            graph_id=data.get("graph_id", ""),
            system_prompt=data.get("system_prompt", ""),
            model=data.get("model", ""),
            skills=data.get("skills", []),
            metadata=data.get("metadata", {}),
        )

    def configure(self, **kwargs) -> None:
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
        logger.info("Agent %s configured: %s", self.name, list(kwargs.keys()))

    async def run(self, client, input_text: str) -> dict:
        if not self.graph_id:
            result = await client.call("agent.create", {
                "name": self.name,
                "system_prompt": self.system_prompt,
                "model": self.model,
                "skills": self.skills,
            })
            self.agent_id = result.get("agent_id", self.agent_id)
            self.graph_id = result.get("graph_id", "")

        result = await client.call("agent.execute", {
            "agent_id": self.agent_id,
            "input": input_text,
        })
        logger.info("Agent %s executed, result keys=%s", self.name, list(result.keys()))
        return result

    async def stream(self, client, input_text: str):
        result = await client.call("agent.execute_stream", {
            "agent_id": self.agent_id,
            "input": input_text,
        })
        if isinstance(result, dict) and "events" in result:
            for event in result["events"]:
                yield event
        else:
            yield result

    async def fork(self, client, input_text: str = "") -> dict:
        result = await client.call("session.fork", {
            "session_id": self.agent_id,
            "input": input_text,
        })
        logger.info("Agent %s forked, bg_session=%s", self.name, result.get("id", ""))
        return result
