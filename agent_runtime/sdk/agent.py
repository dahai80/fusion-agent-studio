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
    # C12: SDK 程序化配置字段 (hook/memory/graph 可达).
    hooks: list[dict] = field(default_factory=list)
    memory: dict = field(default_factory=dict)
    context_window: int = 0
    tools: list[str] = field(default_factory=list)
    max_iterations: int = 0
    temperature: float = 0.0

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
            "hooks": self.hooks,
            "memory": self.memory,
            "context_window": self.context_window,
            "tools": self.tools,
            "max_iterations": self.max_iterations,
            "temperature": self.temperature,
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
            hooks=data.get("hooks", []),
            memory=data.get("memory", {}),
            context_window=data.get("context_window", 0),
            tools=data.get("tools", []),
            max_iterations=data.get("max_iterations", 0),
            temperature=data.get("temperature", 0.0),
        )

    def configure(self, **kwargs) -> None:
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                self.metadata[key] = value
        logger.info("Agent %s configured: %s", self.name, list(kwargs.keys()))

    async def _ensure_created(self, client) -> None:
        # C12: 建 agent (若未建) + 接线 hooks/memory/配置. 幂等.
        if not self.agent_id or not self.graph_id:
            result = await client.call(
                "agent.create",
                {
                    "name": self.name,
                    "system_prompt": self.system_prompt,
                    "model": self.model,
                    "skills": self.skills,
                },
            )
            if "error" in result:
                logger.error("Agent %s create failed: %s", self.name, result["error"])
                return
            self.agent_id = result.get("agent_id", self.agent_id)
            self.graph_id = result.get("graph_id", "")
            logger.info(
                "Agent %s created agent_id=%s graph_id=%s",
                self.name,
                self.agent_id,
                self.graph_id,
            )

        await self._apply_config(client)

    async def _apply_config(self, client) -> None:
        # C12: 按 Agent 字段调 agent.configure + hooks.register + memory.* RPC.
        if not self.agent_id:
            return

        config: dict = {}
        if self.model:
            config["model"] = self.model
        if self.system_prompt:
            config["system_prompt"] = self.system_prompt
        if self.temperature:
            config["temperature"] = self.temperature
        if self.tools:
            config["tools"] = self.tools
        if self.max_iterations:
            config["max_iterations"] = self.max_iterations
        if self.context_window:
            config["context_window"] = self.context_window
        if config:
            cfg_result = await client.call(
                "agent.configure", {"agent_id": self.agent_id, "config": config}
            )
            if "error" in cfg_result:
                logger.warning(
                    "Agent %s configure partial fail: %s",
                    self.name,
                    cfg_result["error"],
                )

        for hook in self.hooks:
            hp = dict(hook)
            hp.setdefault("agent_id", self.agent_id)
            await client.call("hooks.register", hp)

        if self.memory:
            mem_store = self.memory.get("store", {})
            if mem_store:
                await client.call(
                    "memory.store",
                    {"agent_id": self.agent_id, **mem_store},
                )

    async def run(self, client, input_text: str) -> dict:
        await self._ensure_created(client)
        if not self.agent_id:
            return {"error": "Agent creation failed"}

        result = await client.call(
            "agent.execute",
            {
                "agent_id": self.agent_id,
                "input": input_text,
            },
        )
        if isinstance(result, dict) and result.get("graph_id"):
            self.graph_id = result["graph_id"]
        logger.info("Agent %s executed, result keys=%s", self.name, list(result.keys()))
        return result

    def query(self, client, input_text: str, stream: bool = True):
        # C12: SDK 主入口 (类 Claude Agent SDK query()). 同步分发器:
        # stream=True 返回 async generator (供 async for), stream=False 返回
        # coroutine (供 await). 单一 async def 不能既是 generator 又 return 值,
        # 故拆成两个 async helper. 首调建 agent + 接线 hooks/memory/配置.
        if stream:
            return self._query_stream(client, input_text)
        return self._query_result(client, input_text)

    async def _query_stream(self, client, input_text: str):
        await self._ensure_created(client)
        if not self.agent_id:
            return
        async for event in self.stream(client, input_text):
            yield event

    async def _query_result(self, client, input_text: str) -> dict:
        await self._ensure_created(client)
        if not self.agent_id:
            return {"error": "Agent creation failed"}
        return await self.run(client, input_text)

    async def stream(self, client, input_text: str):
        # RPC-collected stream: agent.execute_stream uses execute_graph_stream
        # (stream=True), events include per-token TOKEN events. For true
        # incremental delivery use SSE /v1/graphs/{id}/execute/stream or
        # WS /ws/execute/{graph_id}.
        result = await client.call(
            "agent.execute_stream",
            {
                "agent_id": self.agent_id,
                "input": input_text,
            },
        )
        if isinstance(result, dict) and result.get("graph_id"):
            self.graph_id = result["graph_id"]
        if isinstance(result, dict) and "events" in result:
            for event in result["events"]:
                yield event
        else:
            yield result

    async def fork(self, client, input_text: str = "") -> dict:
        result = await client.call(
            "session.fork",
            {
                "session_id": self.agent_id,
                "input": input_text,
            },
        )
        logger.info("Agent %s forked, bg_session=%s", self.name, result.get("id", ""))
        return result
