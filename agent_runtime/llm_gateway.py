"""LLM Gateway — unified model proxy with fallback routing.

All LLM calls flow through this gateway.  Primary → secondary → tertiary
fallback chain with per-model circuit breaker and load-aware routing.
Pure-offline mode works with only local models; cloud endpoints are optional.
LiteLLM-style embedded proxy: transparent routing layer that normalizes
OpenAI-compatible APIs across local (fusion-mlx) and cloud providers.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

# NetLayer 方案B: 默认经 fusion-gateway :11432 (变量名 GATEWAY 与默认值名副其实)。
# 保留 FUSION_GATEWAY_URL / FUSION_MLX_PORT 显式覆盖 (可回退直连 11434)。
DEFAULT_LOCAL_BASE_URL = os.environ.get(
    "FUSION_GATEWAY_URL", f"http://localhost:{os.environ.get('FUSION_MLX_PORT', '11432')}/v1"
)
CIRCUIT_THRESHOLD = 3
CIRCUIT_RESET_TIME = 30.0


@dataclass
class ModelConfig:
    name: str = ""
    provider: str = "local"
    base_url: str = DEFAULT_LOCAL_BASE_URL
    api_key: str = os.environ.get("FUSION_MLX_API_KEY", "")
    priority: int = 0
    context_length: int = 4096
    capabilities: list[str] = field(default_factory=lambda: ["chat"])
    max_tokens: int = 2048
    temperature: float = 0.7
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "base_url": self.base_url,
            "priority": self.priority,
            "context_length": self.context_length,
            "capabilities": self.capabilities,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelConfig:
        return cls(
            name=data.get("name", ""),
            provider=data.get("provider", "local"),
            base_url=data.get("base_url", DEFAULT_LOCAL_BASE_URL),
            api_key=data.get("api_key", ""),
            priority=data.get("priority", 0),
            context_length=data.get("context_length", 4096),
            capabilities=data.get("capabilities", ["chat"]),
            max_tokens=data.get("max_tokens", 2048),
            temperature=data.get("temperature", 0.7),
            metadata=data.get("metadata", {}),
        )


@dataclass
class ModelStats:
    model_name: str = ""
    requests: int = 0
    successes: int = 0
    failures: int = 0
    total_latency: float = 0.0
    last_request_at: float = 0.0

    @property
    def avg_latency(self) -> float:
        return self.total_latency / self.successes if self.successes else 0.0

    @property
    def error_rate(self) -> float:
        return self.failures / self.requests if self.requests else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "requests": self.requests,
            "successes": self.successes,
            "failures": self.failures,
            "avg_latency": self.avg_latency,
            "error_rate": self.error_rate,
            "last_request_at": self.last_request_at,
        }


class _ModelCircuitBreaker:
    def __init__(
        self, threshold: int = CIRCUIT_THRESHOLD, reset_time: float = CIRCUIT_RESET_TIME
    ):
        self.threshold = threshold
        self.reset_time = reset_time
        self._failures: dict[str, int] = {}
        self._trip_times: dict[str, float] = {}
        self._lock = threading.Lock()

    def record_success(self, model_name: str) -> None:
        with self._lock:
            self._failures.pop(model_name, None)
            self._trip_times.pop(model_name, None)

    def record_failure(self, model_name: str) -> None:
        with self._lock:
            count = self._failures.get(model_name, 0) + 1
            self._failures[model_name] = count
            if count >= self.threshold:
                self._trip_times[model_name] = time.time()
                logger.warning("Circuit breaker TRIPPED for model %s", model_name)

    def is_open(self, model_name: str) -> bool:
        with self._lock:
            if model_name not in self._trip_times:
                return False
            if time.time() - self._trip_times[model_name] >= self.reset_time:
                self._failures.pop(model_name, None)
                self._trip_times.pop(model_name, None)
                logger.info("Circuit breaker RESET for model %s", model_name)
                return False
            return True


@dataclass
class GatewayResponse:
    """Normalized response from the gateway — mirrors FusionMLXClient.LLMResponse."""

    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, Any] = field(
        default_factory=lambda: {
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }
    )
    model: str = ""
    fallback_from: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "tool_calls": self.tool_calls,
            "finish_reason": self.finish_reason,
            "usage": self.usage,
            "model": self.model,
            "fallback_from": self.fallback_from,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GatewayResponse:
        return cls(
            content=data.get("content", ""),
            tool_calls=data.get("tool_calls", []),
            finish_reason=data.get("finish_reason", "stop"),
            usage=data.get("usage", {}),
            model=data.get("model", ""),
            fallback_from=data.get("fallback_from", ""),
        )


class LLMGateway:
    """Unified model proxy with fallback routing and circuit breaker.

    LiteLLM-style embedded proxy that normalizes OpenAI-compatible APIs.
    All LLM calls should go through this gateway — it provides:
    - Priority-based routing with capability matching
    - Automatic fallback chain on failure
    - Per-model circuit breaker
    - Usage statistics tracking
    - Transparent proxy: returns GatewayResponse compatible with LLMResponse
    """

    def __init__(self, default_model: str = "", compactor=None):
        self._models: dict[str, ModelConfig] = {}
        self._stats: dict[str, ModelStats] = {}
        self._cb = _ModelCircuitBreaker()
        self._lock = threading.Lock()
        self._default_model = default_model
        self._default_client: Any = None
        self._compactor = compactor
        logger.info("LLMGateway initialized (default_model=%s)", default_model)

    def set_compactor(self, compactor) -> None:
        self._compactor = compactor
        logger.info("Compactor attached to LLMGateway")

    def _is_context_too_long(self, exc: Exception) -> bool:
        msg = str(exc).lower()
        markers = (
            "context_length",
            "context length",
            "maximum context",
            "too long",
            "too many tokens",
            "prompt is too long",
        )
        return any(m in msg for m in markers)

    def set_default_client(self, client: Any) -> None:
        """Set the default FusionMLXClient for backward compatibility.

        When no model is explicitly routed, the gateway uses this client
        with the default_model name.
        """
        self._default_client = client
        if client and not self._default_model:
            logger.info("Default client set, no default_model specified")
        else:
            logger.info("Default client set with default_model=%s", self._default_model)

    def register_model(self, config: ModelConfig) -> None:
        with self._lock:
            self._models[config.name] = config
            if config.name not in self._stats:
                self._stats[config.name] = ModelStats(model_name=config.name)
        logger.info(
            "Registered model: %s (provider=%s, priority=%d, caps=%s)",
            config.name,
            config.provider,
            config.priority,
            config.capabilities,
        )

    def register_default_local(
        self,
        name: str = "local-default",
        base_url: str = DEFAULT_LOCAL_BASE_URL,
        priority: int = 10,
    ) -> ModelConfig:
        """Convenience: register the default local fusion-mlx model."""
        config = ModelConfig(
            name=name,
            provider="local",
            base_url=base_url,
            priority=priority,
            capabilities=["chat", "completion"],
        )
        self.register_model(config)
        self._default_model = name
        return config

    def unregister_model(self, name: str) -> bool:
        with self._lock:
            removed = self._models.pop(name, None)
            if removed:
                logger.info("Unregistered model: %s", name)
                return True
        return False

    def route(
        self,
        capability: str = "",
        min_context: int = 0,
        exclude: set[str] | None = None,
    ) -> ModelConfig | None:
        with self._lock:
            candidates = [
                m
                for m in self._models.values()
                if not self._cb.is_open(m.name)
                and (not capability or capability in m.capabilities)
                and m.context_length >= min_context
                and (not exclude or m.name not in exclude)
            ]
        if not candidates:
            if self._default_model:
                logger.info(
                    "No registered models, falling back to default_model=%s",
                    self._default_model,
                )
                return ModelConfig(
                    name=self._default_model,
                    base_url=str(self._default_client.base_url) if self._default_client else "",
                    api_key=self._default_client.api_key if self._default_client else "",
                    priority=0,
                    capabilities=set(),
                    context_length=8192,
                )
            logger.warning(
                "No available model for capability='%s' min_context=%d",
                capability,
                min_context,
            )
            return None
        candidates.sort(key=lambda m: (-m.priority, m.name))
        selected = candidates[0]
        logger.debug(
            "Routed to model %s (priority=%d)", selected.name, selected.priority
        )
        return selected

    def get_fallback_chain(
        self, capability: str = "", min_context: int = 0
    ) -> list[ModelConfig]:
        chain = []
        exclude = set()
        while True:
            model = self.route(
                capability=capability, min_context=min_context, exclude=exclude
            )
            if not model:
                break
            chain.append(model)
            exclude.add(model.name)
        return chain

    async def chat(
        self,
        messages: list[dict],
        model: str = "",
        capability: str = "",
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs,
    ) -> GatewayResponse:
        """Primary LLM call interface — drop-in replacement for FusionMLXClient.chat().

        Routes through the gateway with automatic fallback.
        Returns GatewayResponse (compatible with LLMResponse).
        """
        target_config = self._resolve_target(model, capability)
        if not target_config:
            if self._default_client:
                return await self._call_default_client(
                    messages,
                    model=model,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **kwargs,
                )
            return GatewayResponse(
                content="",
                model="",
                finish_reason="error",
                usage={"error": "No available model"},
            )

        start = time.time()
        try:
            result = await self._call_model_async(
                target_config,
                messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )
            latency = time.time() - start
            self._record_success(target_config.name, latency)
            return result
        except Exception as exc:
            latency = time.time() - start
            self._record_failure(target_config.name, latency)
            logger.error("Model %s failed: %s", target_config.name, exc)

            if self._compactor is not None and self._is_context_too_long(exc):
                retry_messages = self._compactor.reactive_strip(messages)
                logger.info(
                    "context-too-long on %s, reactive_strip msgs %d->%d, retrying same model",
                    target_config.name,
                    len(messages),
                    len(retry_messages),
                )
                try:
                    result = await self._call_model_async(
                        target_config,
                        retry_messages,
                        tools=tools,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        **kwargs,
                    )
                    self._record_success(target_config.name, time.time() - start)
                    return result
                except Exception as rx_exc:
                    logger.warning(
                        "reactive retry on %s also failed: %s",
                        target_config.name,
                        rx_exc,
                    )

            for fallback in self.get_fallback_chain(capability=capability):
                if fallback.name == target_config.name:
                    continue
                try:
                    fb_start = time.time()
                    result = await self._call_model_async(
                        fallback,
                        messages,
                        tools=tools,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        **kwargs,
                    )
                    fb_latency = time.time() - fb_start
                    self._record_success(fallback.name, fb_latency)
                    result.fallback_from = target_config.name
                    return result
                except Exception as fb_exc:
                    self._record_failure(fallback.name, time.time() - fb_start)
                    logger.warning(
                        "Fallback model %s also failed: %s", fallback.name, fb_exc
                    )

            if self._default_client:
                logger.info("All models failed, falling back to default client")
                return await self._call_default_client(
                    messages,
                    model=model,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **kwargs,
                )

            return GatewayResponse(
                content="",
                model=target_config.name,
                finish_reason="error",
                usage={"error": str(exc)},
            )

    def execute(
        self,
        messages: list[dict],
        model: str = "",
        capability: str = "",
        tools: list[dict] | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        """Synchronous compatibility wrapper — returns dict (old API)."""
        if model and model in self._models:
            target = self._models[model]
        else:
            target = self.route(capability=capability)
        if not target:
            return {"error": "No available model", "model": None}
        start = time.time()
        try:
            result = self._call_model(target, messages, tools=tools, **kwargs)
            latency = time.time() - start
            self._record_success(target.name, latency)
            return result
        except Exception as exc:
            latency = time.time() - start
            self._record_failure(target.name, latency)
            logger.error("Model %s failed: %s", target.name, exc)
            for fallback in self.get_fallback_chain(capability=capability):
                if fallback.name == target.name:
                    continue
                try:
                    fb_start = time.time()
                    result = self._call_model(fallback, messages, tools=tools, **kwargs)
                    fb_latency = time.time() - fb_start
                    self._record_success(fallback.name, fb_latency)
                    result["fallback_from"] = target.name
                    return result
                except Exception as fb_exc:
                    self._record_failure(fallback.name, time.time() - fb_start)
                    logger.warning(
                        "Fallback model %s also failed: %s", fallback.name, fb_exc
                    )
            return {"error": str(exc), "model": target.name}

    def embed(self, text: str, model: str = "") -> list[float]:
        import asyncio

        target_name = model or ""
        if not target_name:
            emb_model = self.route(capability="embedding")
            target_name = emb_model.name if emb_model else ""

        if not self._default_client:
            logger.warning("No embedding model available, returning stub")
            return self._stub_embedding(text)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            logger.warning("Event loop running; use aembed() instead of embed()")
            return self._stub_embedding(text)

        try:
            return asyncio.run(self.aembed(text, model))
        except Exception as exc:
            logger.warning("Real embedding call failed, using stub: %s", exc)

        logger.warning("No embedding model available, returning stub")
        return self._stub_embedding(text)

    async def aembed(self, text: str, model: str = "") -> list[float]:
        """Async embedding — calls fusion-mlx /v1/embeddings API."""
        target_name = model or ""
        if not target_name:
            emb_model = self.route(capability="embedding")
            target_name = emb_model.name if emb_model else ""

        if self._default_client:
            try:
                results = await self._default_client.embeddings(
                    model=target_name, input=text
                )
                if results and results[0]:
                    logger.info("Real embedding returned, dims=%d", len(results[0]))
                    return results[0]
            except Exception as exc:
                logger.warning("Real embedding call failed, using stub: %s", exc)

        logger.warning("No embedding model available, returning stub")
        return self._stub_embedding(text)

    @staticmethod
    def _stub_embedding(text: str) -> list[float]:
        import math

        h = hash(text) & 0xFFFFFFFF
        rng = h
        vec = []
        for i in range(64):
            rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
            vec.append(math.sin(rng / 1e6))
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def list_models(self) -> list[dict[str, Any]]:
        """List available models — proxies to default client if set."""
        if self._default_client and hasattr(self._default_client, "list_models"):
            try:
                return await self._default_client.list_models()
            except Exception as exc:
                logger.warning("Failed to list models from default client: %s", exc)
        return [
            {"id": m.name, "provider": m.provider, "capabilities": m.capabilities}
            for m in self._models.values()
        ]

    async def unload_model(self, model_id: str) -> bool:
        """Unload a model from fusion-mlx's pool via the default client.

        Returns True on success, False if no default client or the unload
        failed. Always non-fatal: callers use this for the optional per-node
        unload optimization and must not abort the workflow on failure.
        """
        if not model_id:
            return False
        if not self._default_client or not hasattr(
            self._default_client, "unload_model"
        ):
            logger.debug(
                "unload_model skipped: no default client with unload_model, "
                "model=%s",
                model_id,
            )
            return False
        try:
            await self._default_client.unload_model(model_id)
            logger.info("unload_model ok: model=%s", model_id)
            return True
        except Exception as exc:
            logger.warning(
                "unload_model failed (non-fatal): model=%s err=%s",
                model_id,
                exc,
            )
            return False

    async def health(self) -> bool:
        """Health check — proxies to default client if set."""
        if self._default_client and hasattr(self._default_client, "health"):
            try:
                return await self._default_client.health()
            except Exception:
                return False
        return len(self._models) > 0

    async def chat_stream(
        self,
        messages: list[dict],
        model: str = "",
        capability: str = "",
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float = 300.0,
        **kwargs,
    ) -> AsyncIterator[dict]:
        """Streaming LLM call — yields SSE chunks as dicts.

        Each yielded dict has: delta_content, delta_tool_calls, finish_reason.
        Falls back to single-shot if streaming unavailable.
        """
        target_config = self._resolve_target(model, capability)

        client = self._default_client
        resolved_model = self._default_model if model in ("", "default") else model

        if target_config:
            resolved_model = target_config.name
            if target_config.provider != "local" or not client:
                from server.fusion_mlx_client import FusionMLXClient

                client = FusionMLXClient(
                    base_url=target_config.base_url,
                    api_key=target_config.api_key or None,
                )

        if not client:
            yield {
                "delta_content": "",
                "delta_tool_calls": [],
                "finish_reason": "error",
                "error": "No available client",
            }
            return

        temp = temperature if temperature is not None else 0.7
        mtokens = max_tokens if max_tokens is not None else 4096

        try:
            stream_iter = client.chat_stream(
                model=resolved_model,
                messages=messages,
                tools=tools,
                temperature=temp,
                max_tokens=mtokens,
                **kwargs,
            )
            start = asyncio.get_event_loop().time()
            async for chunk in stream_iter:
                elapsed = asyncio.get_event_loop().time() - start
                if elapsed > timeout:
                    logger.warning(
                        "chat_stream exceeded timeout %.0fs, aborting", timeout
                    )
                    yield {
                        "delta_content": "",
                        "delta_tool_calls": [],
                        "finish_reason": "error",
                        "error": f"Stream timeout after {timeout:.0f}s",
                    }
                    return
                yield {
                    "delta_content": chunk.delta_content,
                    "delta_tool_calls": chunk.delta_tool_calls,
                    "finish_reason": chunk.finish_reason,
                }
        except Exception as exc:
            logger.error("chat_stream failed: %s", exc)
            yield {
                "delta_content": "",
                "delta_tool_calls": [],
                "finish_reason": "error",
                "error": str(exc),
            }

    def _resolve_target(
        self, model: str = "", capability: str = ""
    ) -> ModelConfig | None:
        """Resolve which model config to use for a request."""
        if model and model in self._models:
            config = self._models[model]
            if not self._cb.is_open(config.name):
                return config
        return self.route(capability=capability)

    async def _call_model_async(
        self,
        config: ModelConfig,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs,
    ) -> GatewayResponse:
        """Call a model via HTTP — async version returning GatewayResponse."""
        logger.info("Calling model %s at %s", config.name, config.base_url)

        if self._default_client and config.provider == "local":
            try:
                resp = await self._default_client.chat(
                    model=config.name,
                    messages=messages,
                    tools=tools,
                    temperature=temperature
                    if temperature is not None
                    else config.temperature,
                    max_tokens=max_tokens
                    if max_tokens is not None
                    else config.max_tokens,
                )
                return GatewayResponse(
                    content=resp.content,
                    tool_calls=resp.tool_calls,
                    finish_reason=resp.finish_reason,
                    usage=resp.usage,
                    model=config.name,
                )
            except Exception as exc:
                logger.warning(
                    "Default client call failed for %s: %s", config.name, exc
                )
                raise

        try:
            from server.fusion_mlx_client import FusionMLXClient

            client = FusionMLXClient(
                base_url=config.base_url, api_key=config.api_key or None
            )
            response = await client.chat(
                messages=messages,
                model=config.name,
                tools=tools,
                max_tokens=max_tokens if max_tokens is not None else config.max_tokens,
                temperature=temperature
                if temperature is not None
                else config.temperature,
            )
            return GatewayResponse(
                content=response.content,
                tool_calls=response.tool_calls,
                finish_reason=response.finish_reason,
                usage=response.usage,
                model=config.name,
            )
        except ImportError:
            logger.warning("FusionMLXClient not available, returning stub response")
            return GatewayResponse(
                content=f"[stub response from {config.name}]",
                model=config.name,
            )

    async def _call_default_client(
        self,
        messages: list[dict],
        model: str = "",
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs,
    ) -> GatewayResponse:
        """Fall back to the default FusionMLXClient directly."""
        if not self._default_client:
            return GatewayResponse(
                content="",
                model="",
                finish_reason="error",
                usage={"error": "No default client"},
            )
        try:
            resolved_model = self._default_model if model in ("", "default") else model
            resp = await self._default_client.chat(
                model=resolved_model,
                messages=messages,
                tools=tools,
                temperature=temperature if temperature is not None else 0.7,
                max_tokens=max_tokens if max_tokens is not None else 4096,
            )
            return GatewayResponse(
                content=resp.content,
                tool_calls=resp.tool_calls,
                finish_reason=resp.finish_reason,
                usage=resp.usage,
                model=resolved_model,
            )
        except Exception as exc:
            logger.error("Default client call failed: %s", exc)
            return GatewayResponse(
                content="",
                model=resolved_model,
                finish_reason="error",
                usage={"error": str(exc)},
            )

    def _call_model(
        self,
        config: ModelConfig,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        logger.info("Calling model %s at %s", config.name, config.base_url)
        try:
            from server.fusion_mlx_client import FusionMLXClient

            client = FusionMLXClient(
                base_url=config.base_url, api_key=config.api_key or None
            )
            response = client.chat(
                messages=messages,
                model=config.name,
                tools=tools,
                max_tokens=kwargs.get("max_tokens", config.max_tokens),
                temperature=kwargs.get("temperature", config.temperature),
            )
            return {
                "model": config.name,
                "content": response.content if response else "",
                "tool_calls": response.tool_calls if response else [],
                "usage": response.usage if response else {},
            }
        except ImportError:
            logger.warning("FusionMLXClient not available, returning stub response")
            return {
                "model": config.name,
                "content": f"[stub response from {config.name}]",
                "tool_calls": [],
                "usage": {},
            }

    def _record_success(self, model_name: str, latency: float) -> None:
        self._cb.record_success(model_name)
        with self._lock:
            if model_name in self._stats:
                s = self._stats[model_name]
                s.requests += 1
                s.successes += 1
                s.total_latency += latency
                s.last_request_at = time.time()

    def _record_failure(self, model_name: str, latency: float) -> None:
        self._cb.record_failure(model_name)
        with self._lock:
            if model_name in self._stats:
                s = self._stats[model_name]
                s.requests += 1
                s.failures += 1
                s.total_latency += latency
                s.last_request_at = time.time()

    def get_stats(self, model_name: str = "") -> dict[str, Any]:
        if model_name:
            s = self._stats.get(model_name)
            return s.to_dict() if s else {}
        return {name: s.to_dict() for name, s in self._stats.items()}

    def get_model(self, name: str) -> ModelConfig | None:
        return self._models.get(name)
