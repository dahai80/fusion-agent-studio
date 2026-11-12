from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field

from .paths import studio_home

logger = logging.getLogger(__name__)


def _tel_max_spans() -> int:
    # 审计 P1-14/NEW-3: _spans dict 无界 (每 span 永留), 长跑内存泄漏.
    # 默认 10000, FUSION_TELEMETRY_MAX_SPANS 调; 0=不限.
    try:
        return max(0, int(os.environ.get("FUSION_TELEMETRY_MAX_SPANS", "10000")))
    except ValueError:
        return 10000


def _tel_max_traces() -> int:
    # 审计 P1-14/NEW-3: _traces dict 无界. 默认 1000, 0=不限.
    try:
        return max(0, int(os.environ.get("FUSION_TELEMETRY_MAX_TRACES", "1000")))
    except ValueError:
        return 1000


def _tel_max_latencies() -> int:
    # 审计 P1-14/NEW-3: _latencies 每键 list 无界 (每次调用 append). 滚动保留
    # 最近 N 用于 p99/avg, 默认 1000, 0=不限.
    try:
        return max(0, int(os.environ.get("FUSION_TELEMETRY_MAX_LATENCIES", "1000")))
    except ValueError:
        return 1000


# E3: GenAI semantic-conventions mapping (internal span name -> OTel GenAI
# span name + attribute renames). OTLP export emits semconv names so traces
# line up with the cross-vendor GenAI conventions (agent run / LLM inference
# / tool execution).
SEMCONV_SPAN_NAMES = {
    "graph.execute": "invoke_agent",
    "llm.call": "chat",
    "tool.call": "execute_tool",
}


def semconv_span(span_dict: dict) -> dict:
    # Returns the span dict with semconv name + gen_ai.* attributes derived
    # from internal attributes. Non-mapped names pass through unchanged.
    name = span_dict.get("name", "")
    attrs = dict(span_dict.get("attributes") or {})
    if name == "graph.execute":
        out_name = f"invoke_agent {attrs.get('graph_name', '')}".strip()
        attrs["gen_ai.agent.name"] = attrs.get("graph_name", "")
    elif name == "llm.call":
        out_name = "chat"
        if attrs.get("model"):
            attrs["gen_ai.request.model"] = attrs["model"]
        if attrs.get("prompt_tokens"):
            attrs["gen_ai.usage.input_tokens"] = attrs["prompt_tokens"]
        if attrs.get("completion_tokens"):
            attrs["gen_ai.usage.output_tokens"] = attrs["completion_tokens"]
    elif name == "tool.call":
        out_name = f"execute_tool {attrs.get('tool', '')}".strip()
        attrs["gen_ai.tool.name"] = attrs.get("tool", "")
    else:
        out_name = name
    out = dict(span_dict)
    out["name"] = out_name
    out["attributes"] = attrs
    return out


def parse_traceparent(traceparent: str) -> tuple[str, str] | None:
    # W3C traceparent: "00-<32 hex trace id>-<16 hex span id>-<2 hex flags>".
    # Returns (trace_id, span_id) or None on malformed input.
    if not traceparent:
        return None
    parts = traceparent.strip().split("-")
    if len(parts) != 4:
        return None
    version, trace_id, span_id, _flags = parts
    if len(version) != 2 or len(trace_id) != 32 or len(span_id) != 16:
        return None
    if any(x == "0" * len(x) for x in (trace_id, span_id)):
        return None
    try:
        int(version, 16)
        int(trace_id, 16)
        int(span_id, 16)
        int(_flags, 16)
    except ValueError:
        return None
    return trace_id, span_id


def format_traceparent(trace_id: str, span_id: str) -> str:
    return f"00-{trace_id}-{span_id}-01"


class _OtelSdkBridge:
    # E3: optional opentelemetry-sdk export bridge. When the SDK is
    # installed, each completed internal span is re-emitted as a real SDK
    # span (same name/attrs/duration) and the SDK pipeline (batch processor
    # + OTLP exporter) owns transport. In-memory ring buffers stay the
    # local-UI source of truth regardless. Import failure degrades to the
    # hand-written OTLP/HTTP JSON push.
    def __init__(self):
        self.available = False
        self._tracer = None
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider

            provider = trace.get_tracer_provider()
            if not isinstance(provider, trace.TracerProvider):
                provider = TracerProvider(resource=Resource.create({"service.name": "fusion-agent-studio"}))
                trace.set_tracer_provider(provider)
            self._tracer = provider.get_tracer("fusion-agent-studio")
            self._trace_mod = trace
            self.available = True
            logger.info("opentelemetry-sdk bridge armed (in-memory buffers unchanged)")
        except ImportError as e:
            logger.info("opentelemetry-sdk not installed, OTLP JSON push stays: %s", e)
        except Exception as e:
            logger.warning("opentelemetry-sdk bridge init failed, OTLP JSON push stays: %s", e)

    def export_span(self, span_dict: dict) -> None:
        if not self.available or self._tracer is None:
            return
        try:
            sem = semconv_span(span_dict)
            start_ns = int(span_dict.get("start_time", 0.0) * 1e9)
            end_ns = int(span_dict.get("end_time", 0.0) * 1e9)
            span = self._tracer.start_span(
                sem["name"],
                attributes=sem.get("attributes") or {},
                start_time=start_ns,
            )
            span.end(end_time=end_ns)
            logger.debug("sdk bridge exported span %s", sem["name"])
        except Exception as e:
            logger.debug("sdk bridge export failed (non-fatal): %s", e)


@dataclass
class Span:
    trace_id: str = ""
    span_id: str = ""
    parent_id: str = ""
    name: str = ""
    attributes: dict = field(default_factory=dict)
    start_time: float = 0.0
    end_time: float = 0.0
    status: str = "ok"

    def __post_init__(self):
        if not self.span_id:
            self.span_id = uuid.uuid4().hex[:16]
        if not self.start_time:
            self.start_time = time.time()

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "attributes": self.attributes,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": (self.end_time - self.start_time) * 1000
            if self.end_time
            else 0,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Span:
        return cls(
            trace_id=data.get("trace_id", ""),
            span_id=data.get("span_id", ""),
            parent_id=data.get("parent_id", ""),
            name=data.get("name", ""),
            attributes=data.get("attributes", {}),
            start_time=data.get("start_time", 0.0),
            end_time=data.get("end_time", 0.0),
            status=data.get("status", "ok"),
        )


@dataclass
class TelemetryConfig:
    enabled: bool = True
    endpoint: str = ""
    sampling_rate: float = 1.0
    headers: dict = field(default_factory=dict)
    export_format: str = "json"

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "endpoint": self.endpoint,
            "sampling_rate": self.sampling_rate,
            "headers": {k: "***" for k in self.headers},
            "export_format": self.export_format,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TelemetryConfig:
        return cls(
            enabled=data.get("enabled", True),
            endpoint=data.get("endpoint", ""),
            sampling_rate=data.get("sampling_rate", 1.0),
            headers=data.get("headers", {}),
            export_format=data.get("export_format", "json"),
        )


class TelemetryEngine:
    def __init__(self):
        self.config = TelemetryConfig()
        self._spans: dict[str, Span] = {}
        self._traces: dict[str, list[str]] = {}
        self._active_spans: dict[str, Span] = {}
        self._counters: dict[str, int] = {
            "llm_calls": 0,
            "tool_calls": 0,
            "graph_executions": 0,
            "errors": 0,
            "tokens_prompt": 0,
            "tokens_completion": 0,
            # #483: reasoning split for reasoning models (N8 #345 residual)
            "tokens_reasoning": 0,
            # N11 #350: long-session SLO counters
            "compactions": 0,
            "approvals": 0,
            "sessions_completed": 0,
        }
        self._latencies: dict[str, list[float]] = {
            "llm_calls": [],
            "tool_calls": [],
            "graph_executions": [],
            # N11 #350: approval wait + full session duration (seconds)
            "approvals": [],
            "sessions": [],
        }
        # #486: point-in-time gauges (approval noise etc.) — refreshed by
        # callers from live engine state, exposed via metrics().
        self._gauges: dict[str, float] = {}
        # E3: optional opentelemetry-sdk bridge — armed when importable and
        # FUSION_OTEL_SDK != 0. In-memory buffers stay authoritative locally.
        self.sdk_bridge: _OtelSdkBridge | None = (
            None
            if os.environ.get("FUSION_OTEL_SDK", "1").strip().lower() in ("0", "off", "false")
            else _OtelSdkBridge()
        )
        logger.info("TelemetryEngine initialized")

    def _prune_spans(self) -> None:
        # 审计 P1-14/NEW-3: _spans 超 cap 删最旧 (按 start_time). 不删 active.
        cap = _tel_max_spans()
        if cap <= 0 or len(self._spans) <= cap:
            return
        candidates = [
            (sid, s.start_time) for sid, s in self._spans.items()
            if sid not in self._active_spans
        ]
        candidates.sort(key=lambda kv: kv[1])
        overflow = len(self._spans) - cap
        for sid, _ in candidates[:overflow]:
            self._spans.pop(sid, None)
        logger.debug("telemetry pruned %d old spans (cap=%d)", min(overflow, len(candidates)), cap)

    def _prune_traces(self) -> None:
        # 审计 P1-14/NEW-3: _traces 超 cap 删最旧 trace (整条链 span_ids 从 _spans 清).
        cap = _tel_max_traces()
        if cap <= 0 or len(self._traces) <= cap:
            return
        # 按 trace 内最新 span start_time 排序 (无 span 则 0), 删最旧.
        ranked = []
        for tid, sids in self._traces.items():
            latest = max(
                (self._spans[s].start_time for s in sids if s in self._spans),
                default=0.0,
            )
            ranked.append((tid, latest))
        ranked.sort(key=lambda kv: kv[1])
        overflow = len(self._traces) - cap
        for tid, _ in ranked[:overflow]:
            for sid in self._traces.pop(tid, []):
                self._spans.pop(sid, None)
        logger.debug("telemetry pruned %d old traces (cap=%d)", overflow, cap)

    def _prune_latencies(self, key: str) -> None:
        # 审计 P1-14/NEW-3: 单键 latency list 超 cap 保留最近 N.
        cap = _tel_max_latencies()
        vals = self._latencies.get(key)
        if cap <= 0 or not vals or len(vals) <= cap:
            return
        self._latencies[key] = vals[-cap:]
        logger.debug("telemetry pruned %s latencies to %d (cap=%d)", key, cap, cap)

    def configure(self, params: dict) -> None:
        self.config = TelemetryConfig.from_dict(params)
        logger.info(
            "Telemetry configured: enabled=%s endpoint=%s sampling=%.2f",
            self.config.enabled,
            self.config.endpoint or "none",
            self.config.sampling_rate,
        )

    def start_span(
        self,
        name: str,
        trace_id: str | None = None,
        parent_id: str = "",
        attributes: dict | None = None,
    ) -> Span:
        if not self.config.enabled:
            return Span(name=name, trace_id=trace_id or "", parent_id=parent_id)

        if not trace_id:
            trace_id = uuid.uuid4().hex[:16]

        span = Span(
            trace_id=trace_id,
            parent_id=parent_id,
            name=name,
            attributes=attributes or {},
        )
        self._spans[span.span_id] = span
        self._active_spans[span.span_id] = span

        if trace_id not in self._traces:
            self._traces[trace_id] = []
        self._traces[trace_id].append(span.span_id)

        # 审计 P1-14/NEW-3: span/trace 入表后裁剪, 防无界增长.
        self._prune_spans()
        self._prune_traces()

        logger.debug("Started span %s name=%s trace=%s", span.span_id, name, trace_id)
        return span

    def end_span(self, span_id: str, status: str = "ok") -> Span | None:
        span = self._active_spans.pop(span_id, None)
        if not span:
            logger.warning("Attempted to end unknown span %s", span_id)
            return None
        span.end_time = time.time()
        span.status = status

        if span.name == "llm.call":
            self._counters["llm_calls"] += 1
            self._latencies["llm_calls"].append(span.end_time - span.start_time)
            self._counters["tokens_prompt"] += span.attributes.get("prompt_tokens", 0)
            self._counters["tokens_completion"] += span.attributes.get(
                "completion_tokens", 0
            )
            self._counters["tokens_reasoning"] += span.attributes.get(
                "reasoning_tokens", 0
            )
            self._prune_latencies("llm_calls")
        elif span.name == "tool.call":
            self._counters["tool_calls"] += 1
            self._latencies["tool_calls"].append(span.end_time - span.start_time)
            self._prune_latencies("tool_calls")
        elif span.name == "graph.execute":
            self._counters["graph_executions"] += 1
            self._latencies["graph_executions"].append(span.end_time - span.start_time)
            self._prune_latencies("graph_executions")

        if status == "error":
            self._counters["errors"] += 1

        logger.debug(
            "Ended span %s duration=%.3fms status=%s",
            span_id,
            (span.end_time - span.start_time) * 1000,
            status,
        )
        # E3: SDK bridge fan-out (non-fatal; in-memory record already done).
        if self.sdk_bridge is not None and self.sdk_bridge.available:
            try:
                self.sdk_bridge.export_span(span.to_dict())
            except Exception as e:
                logger.debug("sdk bridge export error: %s", e)
        return span

    def span_from_traceparent(
        self,
        name: str,
        traceparent: str,
        attributes: dict | None = None,
    ) -> Span | None:
        # E3: continue an inbound W3C trace — internal trace_id = first 16
        # hex of the upstream trace id, parent_id = upstream span id, so the
        # local chain visually joins the upstream trace in any receiver.
        parsed = parse_traceparent(traceparent)
        if parsed is None:
            logger.warning("invalid traceparent, span started unlinked: %s", traceparent[:60])
            return None
        ext_trace, ext_span = parsed
        attrs = dict(attributes or {})
        attrs["w3c.traceparent"] = traceparent.strip()
        attrs["otel.trace_id"] = ext_trace
        return self.start_span(
            name, trace_id=ext_trace[:16], parent_id=ext_span, attributes=attrs
        )

    def get_trace(self, trace_id: str) -> dict | None:
        span_ids = self._traces.get(trace_id)
        if not span_ids:
            return None
        spans = [self._spans[sid].to_dict() for sid in span_ids if sid in self._spans]
        return {"trace_id": trace_id, "spans": spans, "span_count": len(spans)}

    def list_spans(self, trace_id: str | None = None, limit: int = 100) -> list[dict]:
        if trace_id:
            span_ids = self._traces.get(trace_id, [])
            spans = [
                self._spans[sid].to_dict() for sid in span_ids if sid in self._spans
            ]
        else:
            spans = [s.to_dict() for s in self._spans.values()]
        spans.sort(key=lambda s: s.get("start_time", 0), reverse=True)
        return spans[:limit]

    def export(self, fmt: str = "json", push: bool = False) -> str:
        if fmt == "json":
            data = {
                "config": self.config.to_dict(),
                "spans": [s.to_dict() for s in self._spans.values()],
                "traces": {tid: sids for tid, sids in self._traces.items()},
                "counters": dict(self._counters),
            }
            return json.dumps(data, indent=2, ensure_ascii=False)
        elif fmt == "otlp":
            resource_spans = []
            for trace_id, span_ids in self._traces.items():
                scope_spans = []
                for sid in span_ids:
                    span = self._spans.get(sid)
                    if span:
                        # E3: GenAI semconv names + gen_ai.* attributes on
                        # the wire so Jaeger/Tempo line up cross-vendor.
                        scope_spans.append(semconv_span(span.to_dict()))
                resource_spans.append(
                    {
                        "resource": {
                            "attributes": {"service.name": "fusion-agent-studio"}
                        },
                        "scopeSpans": [{"spans": scope_spans}],
                    }
                )
            payload = json.dumps(
                {"resourceSpans": resource_spans}, indent=2, ensure_ascii=False
            )
            # C13: endpoint 非空时 HTTP POST resourceSpans 到 OTLP HTTP JSON
            # receiver (标准 /v1/traces OTLP/HTTP). 无 opentelemetry-sdk 依赖,
            # 兼容 Jaeger/Tempo/OTel-collector HTTP JSON 入口.
            if push and self.config.endpoint:
                self._push_otlp(payload)
            return payload
        elif fmt == "console":
            lines = ["=== Telemetry Export ==="]
            for span in self._spans.values():
                d = span.to_dict()
                lines.append(
                    f"  [{d['name']}] trace={d['trace_id']} span={d['span_id']} "
                    f"duration={d['duration_ms']:.1f}ms status={d['status']}"
                )
            return "\n".join(lines)
        else:
            logger.warning("Unknown export format '%s', falling back to json", fmt)
            return self.export("json")

    def _push_otlp(self, payload: str) -> None:
        # C13: POST OTLP/HTTP JSON 到 collector. 同步 urllib 避免新依赖;
        # 失败仅日志不抛 (遥测不阻塞主路径).
        # 审计 P1-14/NEW-3: 检测 event loop 在跑则 offload 到 executor (5s 同步
        # urllib 在 async handler 内直接阻塞 event loop); 无 loop 则同步直调.
        import urllib.error
        import urllib.request

        endpoint = self.config.endpoint
        headers = {"Content-Type": "application/json"}
        headers.update(self.config.headers)

        def _do_post() -> None:
            req = urllib.request.Request(
                endpoint,
                data=payload.encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    logger.info(
                        "OTLP export to %s -> HTTP %d (bytes=%d)",
                        endpoint,
                        resp.status,
                        len(payload),
                    )
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                logger.warning("OTLP export to %s failed: %s", endpoint, e)

        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.run_in_executor(None, _do_post)
            logger.debug("OTLP export offloaded to executor (async context)")
        else:
            _do_post()

    # N11 #350: long-session SLO recording — session duration, compaction
    # count, approval latency feed fusion-bench baselines via metrics().
    def record_slo(self, kind: str, value: float | None = None) -> None:
        if kind == "compaction":
            self._counters["compactions"] += 1
        elif kind == "approval":
            self._counters["approvals"] += 1
            if value is not None:
                self._latencies["approvals"].append(value)
                self._prune_latencies("approvals")
        elif kind == "session":
            self._counters["sessions_completed"] += 1
            if value is not None:
                self._latencies["sessions"].append(value)
                self._prune_latencies("sessions")
        else:
            logger.warning("record_slo unknown kind=%s", kind)

    def record_permission_noise(self, stats: dict) -> float:
        # #486: approval-noise metric — asks per 100 permission decisions.
        # Reproducible from telemetry.metrics() instead of a one-off claim;
        # the baseline/armed delta is proven by tests/proof/prove_approval_noise.py.
        asks = int(stats.get("asks", 0) or 0)
        auto = int(stats.get("auto_allow_readonly", 0) or 0) + int(
            stats.get("auto_allow_loopback", 0) or 0
        )
        denies = int(stats.get("denies", 0) or 0)
        total = asks + auto + denies
        rate = (asks / total * 100.0) if total else 0.0
        self._gauges["approval_asks_per_100_calls"] = rate
        self._gauges["permission_decisions"] = float(total)
        logger.debug(
            "permission noise gauge: asks=%d auto=%d denies=%d rate=%.1f",
            asks, auto, denies, rate,
        )
        return rate

    def slo_summary(self) -> dict:
        avg_latencies = {}
        for key, vals in self._latencies.items():
            if vals:
                avg_latencies[f"avg_{key}_ms"] = sum(vals) / len(vals) * 1000
                avg_latencies[f"p99_{key}_ms"] = sorted(vals)[int(len(vals) * 0.99)] * 1000
            else:
                avg_latencies[f"avg_{key}_ms"] = 0
                avg_latencies[f"p99_{key}_ms"] = 0
        return {
            "sessions_completed": self._counters.get("sessions_completed", 0),
            "compactions_total": self._counters.get("compactions", 0),
            "approvals_total": self._counters.get("approvals", 0),
            "avg_approval_latency_ms": avg_latencies["avg_approvals_ms"],
            "p99_approval_latency_ms": avg_latencies["p99_approvals_ms"],
            "avg_session_duration_ms": avg_latencies["avg_sessions_ms"],
            "p99_session_duration_ms": avg_latencies["p99_sessions_ms"],
        }

    def export_slo(self, path: str | None = None) -> dict:
        # #387: producer-side baseline export for fusion-bench consumption.
        # One JSONL record per call: schema version + timestamp + summary.
        import json
        import os

        target = path or os.environ.get(
            "FUSION_SLO_BASELINE_PATH",
            os.path.join(
                str(studio_home()), "slo_baselines.jsonl"
            ),
        )
        record = {"schema": 1, "ts": time.time(), **self.slo_summary()}
        try:
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(target, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            logger.info("SLO baseline exported records_path=%s", target)
            return {"path": target, "record": record}
        except OSError as e:
            logger.error("SLO baseline export failed path=%s err=%s", target, e)
            return {"path": target, "error": str(e), "record": record}

    def metrics(self) -> dict:
        avg_latencies = {}
        for key, vals in self._latencies.items():
            if vals:
                avg_latencies[f"avg_{key}_ms"] = sum(vals) / len(vals) * 1000
                avg_latencies[f"p99_{key}_ms"] = (
                    sorted(vals)[int(len(vals) * 0.99)] * 1000
                )
            else:
                avg_latencies[f"avg_{key}_ms"] = 0
                avg_latencies[f"p99_{key}_ms"] = 0

        return {
            "counters": dict(self._counters),
            "latencies": avg_latencies,
            # #486: gauges — approval_asks_per_100_calls + decision count.
            "gauges": dict(self._gauges),
            "total_spans": len(self._spans),
            "total_traces": len(self._traces),
            "active_spans": len(self._active_spans),
        }
