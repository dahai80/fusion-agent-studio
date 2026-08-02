"""Sub-dispatcher: InfraDispatcher."""
from __future__ import annotations
import logging
from typing import Any
from .base import SubDispatcher

logger = logging.getLogger(__name__)


class InfraDispatcher(SubDispatcher):
    async def _handle_telemetry_configure(self, params: dict) -> dict:
        engine = self._daemon._get_telemetry_engine()
        engine.configure(params)
        logger.info("telemetry.configure: enabled=%s", params.get("enabled", True))
        return {"configured": True}

    async def _handle_telemetry_get_trace(self, params: dict) -> dict:
        engine = self._daemon._get_telemetry_engine()
        trace_id = params.get("trace_id", "")
        trace = engine.get_trace(trace_id)
        if not trace:
            raise ValueError(f"Trace not found: {trace_id}")
        return trace

    async def _handle_telemetry_export(self, params: dict) -> dict:
        engine = self._daemon._get_telemetry_engine()
        fmt = params.get("format", "json")
        data = engine.export(fmt)
        logger.info("telemetry.export: format=%s size=%d", fmt, len(data))
        return {"format": fmt, "data": data}

    async def _handle_telemetry_list_spans(self, params: dict) -> dict:
        engine = self._daemon._get_telemetry_engine()
        trace_id = params.get("trace_id")
        limit = params.get("limit", 100)
        spans = engine.list_spans(trace_id=trace_id, limit=limit)
        return {"spans": spans}

    async def _handle_telemetry_metrics(self, params: dict) -> dict:
        engine = self._daemon._get_telemetry_engine()
        metrics = engine.metrics()
        return metrics

    async def _handle_connector_list(self, params: dict) -> dict:
        mgr = self._daemon._get_connector_manager()
        return {"connectors": mgr.list_connectors()}

    async def _handle_connector_create(self, params: dict) -> dict:
        mgr = self._daemon._get_connector_manager()
        name = params.get("name", "")
        if not name:
            return {"status": "error", "message": "name parameter required"}
        return mgr.create(name, params.get("type", "api_key"), params.get("auth_config", {}))

    async def _handle_connector_get(self, params: dict) -> dict:
        mgr = self._daemon._get_connector_manager()
        connector_id = params.get("connector_id", "")
        result = mgr.get(connector_id)
        if result is None:
            return {"status": "error", "message": f"Connector not found: {connector_id}"}
        return {"connector": result}

    async def _handle_connector_update(self, params: dict) -> dict:
        mgr = self._daemon._get_connector_manager()
        connector_id = params.get("connector_id", "")
        return mgr.update(connector_id, params.get("updates", {}))

    async def _handle_connector_delete(self, params: dict) -> dict:
        mgr = self._daemon._get_connector_manager()
        connector_id = params.get("connector_id", "")
        return mgr.delete(connector_id)

    async def _handle_connector_connect(self, params: dict) -> dict:
        mgr = self._daemon._get_connector_manager()
        connector_id = params.get("connector_id", "")
        return mgr.connect(connector_id)

    async def _handle_connector_disconnect(self, params: dict) -> dict:
        mgr = self._daemon._get_connector_manager()
        connector_id = params.get("connector_id", "")
        return mgr.disconnect(connector_id)

    async def _handle_connector_test(self, params: dict) -> dict:
        mgr = self._daemon._get_connector_manager()
        connector_id = params.get("connector_id", "")
        return mgr.test(connector_id)

    # ── Dashboard handler ──

    async def _handle_apikey_create(self, params: dict) -> dict:
        mgr = self._daemon._get_apikey_manager()
        name = params.get("name", "")
        if not name:
            return {"status": "error", "message": "name parameter required"}
        return mgr.create(
            name=name,
            permissions=params.get("permissions"),
            allowed_agent_ids=params.get("allowed_agent_ids"),
            ip_whitelist=params.get("ip_whitelist"),
            expires_at=params.get("expires_at"),
        )

    async def _handle_apikey_list(self, params: dict) -> dict:
        mgr = self._daemon._get_apikey_manager()
        return {"keys": mgr.list_keys()}

    async def _handle_apikey_revoke(self, params: dict) -> dict:
        mgr = self._daemon._get_apikey_manager()
        key_id = params.get("key_id", "")
        return mgr.revoke(key_id)

    async def _handle_apikey_rotate(self, params: dict) -> dict:
        mgr = self._daemon._get_apikey_manager()
        key_id = params.get("key_id", "")
        return mgr.rotate(key_id)

    async def _handle_apikey_update(self, params: dict) -> dict:
        mgr = self._daemon._get_apikey_manager()
        key_id = params.get("key_id", "")
        return mgr.update(key_id, params.get("updates", {}))

    # ── Analytics handler ──

    async def _handle_cron_register(self, params: dict) -> dict:
        from .triggers import CronJob
        cm = self._daemon._get_cron_manager()
        job_id = params.get("id", f"cron_{int(time.time())}")
        job = CronJob(
            id=job_id,
            name=params.get("name", ""),
            expression=params.get("expression", "* * * * *"),
            graph_id=params.get("graph_id", ""),
            enabled=params.get("enabled", True),
            input_data=params.get("input_data", ""),
            max_retries=params.get("max_retries", 0),
        )
        await cm.aregister(job)
        return {"status": "ok", "job": job.to_dict()}

    async def _handle_cron_unregister(self, params: dict) -> dict:
        job_id = params.get("id", "")
        if not job_id:
            return {"status": "error", "message": "id parameter required"}
        cm = self._daemon._get_cron_manager()
        await cm.aunregister(job_id)
        return {"status": "ok", "unregistered": job_id}

    async def _handle_cron_list(self, params: dict) -> dict:
        cm = self._daemon._get_cron_manager()
        return {"jobs": cm.list()}

    async def _handle_cron_list_executions(self, params: dict) -> dict:
        cm = self._daemon._get_cron_manager()
        job_id = params.get("job_id", "")
        limit = params.get("limit", 20)
        return {"executions": await cm.alist_executions(job_id=job_id, limit=limit)}

    # ── Dynamic tool handlers ──

    _SAFE_TOOL_NAME_RE = __import__("re").compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

    async def _handle_hooks_list(self, params: dict) -> dict:
        engine = self._daemon._get_hooks()
        return {"hooks": engine.list_hooks()}

    async def _handle_hooks_register(self, params: dict) -> dict:
        from .hooks import HookConfig
        engine = self._daemon._get_hooks()
        hook = HookConfig.from_dict(params)
        engine.register(hook)
        logger.info("hooks.register event=%s matcher=%s", hook.event, hook.matcher)
        return {"ok": True, "hook": hook.to_dict()}

    async def _handle_hooks_test(self, params: dict) -> dict:
        engine = self._daemon._get_hooks()
        event = params.get("event", "")
        payload = params.get("payload", {})
        result = await engine.fire(event, payload, tool_name=params.get("tool_name", ""))
        return {"result": self._daemon._serialize(result)}

    async def _handle_sdk_list_types(self, params: dict) -> dict:
        from .sdk import list_available_types
        types = list_available_types()
        return {"types": types}

    async def _handle_sdk_verify(self, params: dict) -> dict:
        from .sdk import verify_agent
        agent_def = params.get("agent", {})
        result = verify_agent(agent_def)
        return result

    async def _handle_sdk_scaffold(self, params: dict) -> dict:
        from .sdk import scaffold_agent
        result = scaffold_agent(
            name=params.get("name", "my_agent"),
            template=params.get("template", "basic"),
            output_dir=params.get("output_dir", ""),
        )
        return result

    async def _handle_alert_list(self, params: dict) -> dict:
        alerts = []
        try:
            budget_handler = self._daemon._handle_budget_status({})
            budget_data = budget_handler if isinstance(budget_handler, dict) else {}
            warn_pct = budget_data.get("warn_percent", 0)
            if warn_pct > 80:
                alerts.append({"id": "budget-warning", "level": "warning", "message": f"Token budget usage at {warn_pct}%", "type": "budget", "acknowledged": False})
            if warn_pct > 95:
                alerts.append({"id": "budget-critical", "level": "critical", "message": f"Token budget nearly exhausted ({warn_pct}%)", "type": "budget", "acknowledged": False})
        except Exception:
            pass
        try:
            sessions = self._daemon.store.list_sessions()
            _now = time.time()
            for s in sessions[-20:]:
                if isinstance(s, dict) and s.get("status") == "error":
                    alerts.append({"id": f"session-error-{s.get('session_id', '')}", "level": "error", "message": f"Session error: {s.get('error', 'unknown')}", "type": "session", "acknowledged": False})
        except Exception:
            pass
        return {"alerts": alerts}

    async def _handle_alert_acknowledge(self, params: dict) -> dict:
        alert_id = params.get("alert_id", "")
        logger.info("alert.acknowledge: id=%s", alert_id)
        return {"acknowledged": True, "alert_id": alert_id}

    # ── Marketplace handlers ──

    async def _handle_dashboard_overview(self, params: dict) -> dict:
        self._daemon._load_agents_index()
        total_agents = len(self._daemon._agents)
        published_agents = sum(1 for m in self._daemon._agents.values() if m.get("status") == "published")
        active_agents = sum(1 for m in self._daemon._agents.values() if m.get("status") in ("draft", "published"))

        today_requests = 0
        total_tokens = 0
        error_count = 0
        try:
            sessions = self._daemon.store.list_sessions()
            _now = time.time()
            day_ago = _now - 86400
            for s in sessions:
                ts = s.get("timestamp", 0) if isinstance(s, dict) else 0
                if ts > day_ago:
                    today_requests += 1
                if isinstance(s, dict) and s.get("status") == "error":
                    error_count += 1
        except Exception as exc:
            logger.warning("dashboard.overview session query failed: %s", exc)

        try:
            from .metrics_engine import MetricsEngine
            me = MetricsEngine()
            summary = me.get_summary()
            total_tokens = summary.total_tokens_in + summary.total_tokens_out
        except Exception as exc:
            logger.warning("dashboard.overview metrics query failed: %s", exc)

        alerts = []
        try:
            budget_handler = self._daemon._handle_budget_status({})
            budget_data = budget_handler if isinstance(budget_handler, dict) else {}
            warn_pct = budget_data.get("warn_percent", 0)
            if warn_pct > 80:
                alerts.append({"level": "warning", "message": f"Token budget usage at {warn_pct}%", "type": "budget"})
        except Exception:
            pass

        recent_agents = []
        sorted_agents = sorted(self._daemon._agents.items(), key=lambda x: x[1].get("created_at", 0), reverse=True)[:5]
        for aid, meta in sorted_agents:
            recent_agents.append({"id": aid, "name": meta.get("name", ""), "status": meta.get("status", "draft")})

        logger.info("dashboard.overview: agents=%d requests=%d tokens=%d errors=%d", total_agents, today_requests, total_tokens, error_count)
        return {
            "total_agents": total_agents,
            "published_agents": published_agents,
            "active_agents": active_agents,
            "today_requests": today_requests,
            "total_tokens": total_tokens,
            "error_count": error_count,
            "alerts": alerts,
            "recent_agents": recent_agents,
        }

    # ── API Key handlers ──

    async def _handle_analytics_agent_usage(self, params: dict) -> dict:
        agent_id = params.get("agent_id")
        time_range = params.get("time_range", "day")
        now = time.time()
        range_seconds = {"day": 86400, "week": 604800, "month": 2592000}.get(time_range, 86400)
        cutoff = now - range_seconds

        agents_usage = []
        try:
            from .metrics_engine import MetricsEngine
            me = MetricsEngine()
            sessions = me.query_sessions()
            agent_buckets: dict[str, dict] = {}
            for s in sessions:
                ts = s.timestamp if hasattr(s, "timestamp") else s.get("timestamp", 0)
                if ts < cutoff:
                    continue
                gid = s.graph_id if hasattr(s, "graph_id") else s.get("graph_id", "unknown")
                aid = gid if agent_id is None else agent_id
                if agent_id and gid != agent_id:
                    continue
                bucket = agent_buckets.setdefault(aid, {"agent_id": aid, "requests": 0, "input_tokens": 0, "output_tokens": 0, "errors": 0})
                bucket["requests"] += 1
                if hasattr(s, "error") and s.error:
                    bucket["errors"] += 1
                elif isinstance(s, dict) and s.get("error"):
                    bucket["errors"] += 1
            agents_usage = list(agent_buckets.values())
        except Exception as exc:
            logger.warning("analytics.agent_usage failed: %s", exc)

        logger.info("analytics.agent_usage: range=%s agents=%d", time_range, len(agents_usage))
        return {"agents": agents_usage, "time_range": time_range}

    # ── Style handlers ──

    async def _handle_model_status(self, params: dict) -> dict:
        running = self._daemon._mlx_process is not None and self._daemon._mlx_process.poll() is None
        connected = False
        models = []
        loaded = []
        url = f"http://localhost:{MLX_PORT}"
        if running:
            connected = await self._daemon._check_mlx_health()
            models = await self._daemon._list_mlx_models()
            loaded = [m for m in models if m.get("loaded", False)]
        return {
            "connected": connected,
            "models": models,
            "loaded": loaded,
            "url": url,
        }

    async def _handle_audit_list(self, params: dict) -> dict:
        tool = params.get("tool", "")
        target_type = params.get("target_type", "")
        since = params.get("since", "")
        limit = params.get("limit", 50)
        logger_instance = self._daemon._get_audit_logger()
        kwargs = {"limit": limit}
        if tool:
            kwargs["tool"] = tool
        if target_type:
            kwargs["target_type"] = target_type
        if since:
            kwargs["since"] = since
        result = logger_instance.query_logs(**kwargs)
        if isinstance(result, dict):
            return result
        entries = [e.to_dict() if hasattr(e, "to_dict") else e for e in (result or [])]
        return {"data": entries, "total": len(entries)}

    async def _handle_system_offline_status(self, params: dict) -> dict:
        import os
        env_offline = os.environ.get("FUSION_CODE_OFFLINE", "").lower() in ("1", "true", "yes")
        offline = self._daemon._offline_mode or env_offline
        reason = None
        if env_offline:
            reason = "FUSION_CODE_OFFLINE environment variable set"
        elif self._daemon._offline_mode:
            reason = "Manually enabled via system.set_offline"
        return {"offline": offline, "reason": reason}

    async def _handle_system_set_offline(self, params: dict) -> dict:
        enabled = params.get("enabled", False)
        self._daemon._offline_mode = bool(enabled)
        logger.info("system.set_offline: offline=%s", self._daemon._offline_mode)
        return {"offline": self._daemon._offline_mode}
