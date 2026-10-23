"""Single audited capability dispatch door (#437 / E1, §5.2/§5.3/§5.5).

Port of f-ecommerce web/api.py /agent-platform/call — one try/finally
around lookup -> policy -> validation -> dispatch so EVERY post-identity
outcome (including denials) is audited.

Generalizations:
- runs in-process on the studio daemon (host), no console proxy hop;
- mutations create a persistent L3 approval (CapabilityApprovalStore,
  SQLite) — a human decides via agent_platform.approve/reject (the
  approval workbench card surface), requester != approver enforced;
- publisher authority resolves SERVER-SIDE from the publish registry
  (written at publish time from the authenticated caller) — the body's
  publisher field is only a fallback;
- rest routes dispatch module×route through the gateway /shell/<module>/
  proxy (module key injection) or the local api server when no gateway
  is configured.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .capabilities import get_capability, validate_params

logger = logging.getLogger(__name__)

WRITE_ROLES = ("admin", "owner", "operator")
_APPROVAL_TTL_SECONDS = 7 * 24 * 3600


class DoorError(Exception):
    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(str(detail))


class CapabilityApprovalStore:
    """SQLite-backed approval records (L3 workbench).

    #446: two kinds share one table — 'mutation' (created by the door when
    an agent calls a mutation capability; approve dispatches the payload)
    and 'external' (created via REST by outside consumers such as
    EnviPermit; approve/reject only records the decision + fires
    webhooks — nothing dispatches). External records carry an assigned
    reviewer and a display title; the requester != approver invariant is
    enforced for both kinds.
    """

    def __init__(self, db_path: Optional[str] = None):
        if not db_path:
            db_path = str(
                Path.home() / ".fusion-agent-studio" / "capability_approvals.db")
        self.db_path = str(db_path)
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS capability_approvals (
               id TEXT PRIMARY KEY,
               capability_id TEXT NOT NULL,
               agent_id TEXT NOT NULL,
               version INTEGER NOT NULL DEFAULT 0,
               publisher TEXT DEFAULT '',
               params TEXT NOT NULL DEFAULT '{}',
               status TEXT NOT NULL DEFAULT 'pending',
               requester TEXT DEFAULT '',
               decided_by TEXT DEFAULT '',
               created_at REAL NOT NULL,
               decided_at REAL,
               result TEXT DEFAULT '')""")
        # #446 migration: older stores predate the external-task columns.
        existing = {row[1] for row in conn.execute(
            "PRAGMA table_info(capability_approvals)").fetchall()}
        for column, default in (("kind", "'mutation'"),
                                ("assigned_to", "''"),
                                ("title", "''")):
            if column not in existing:
                conn.execute(
                    f"ALTER TABLE capability_approvals ADD COLUMN "
                    f"{column} TEXT DEFAULT {default}")
        conn.commit()
        conn.close()
        logger.info("capability approval store ready: %s", self.db_path)

    def create(self, capability_id: str, agent_id: str, version: int,
               publisher: str, params: dict, requester: str) -> dict:
        approval_id = uuid.uuid4().hex[:16]
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO capability_approvals VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (approval_id, capability_id, agent_id, int(version),
             publisher, json.dumps(params, ensure_ascii=False), "pending",
             requester, "", time.time(), None, "", "mutation", "", ""))
        conn.commit()
        conn.close()
        return self.get(approval_id)

    def create_external(self, title: str, requester: str, assigned_to: str,
                        params: dict, agent_id: str = "") -> dict:
        """#446: external approval task — an outside consumer's review item.

        The payload (`params`) is opaque to the studio: consumers put their
        own object references (project/doc version IDs, evidence summary)
        there; the decision webhook returns it verbatim.
        """
        if requester and assigned_to and requester == assigned_to:
            raise DoorError(422, "requester and assigned reviewer must "
                                 "differ (no self-approval)")
        approval_id = uuid.uuid4().hex[:16]
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO capability_approvals VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (approval_id, "external", agent_id or "external", 0, "",
             json.dumps(params, ensure_ascii=False), "pending",
             requester, "", time.time(), None, "", "external",
             assigned_to, title))
        conn.commit()
        conn.close()
        logger.info("external approval task created: %s requester=%s "
                    "assigned_to=%s", approval_id, requester, assigned_to)
        return self.get(approval_id)

    def get(self, approval_id: str) -> Optional[dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM capability_approvals WHERE id = ?",
            (approval_id,)).fetchone()
        conn.close()
        if not row:
            return None
        rec = dict(row)
        rec["params"] = json.loads(rec.get("params") or "{}")
        return rec

    def list(self, status: str = "", limit: int = 100) -> List[dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        if status:
            rows = conn.execute(
                "SELECT * FROM capability_approvals WHERE status = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM capability_approvals "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        conn.close()
        out = []
        for row in rows:
            rec = dict(row)
            rec["params"] = json.loads(rec.get("params") or "{}")
            out.append(rec)
        return out

    def decide(self, approval_id: str, status: str, decided_by: str,
               result: str = "") -> Optional[dict]:
        rec = self.get(approval_id)
        if not rec:
            return None
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE capability_approvals SET status = ?, decided_by = ?, "
            "decided_at = ?, result = ? WHERE id = ?",
            (status, decided_by, time.time(), result, approval_id))
        conn.commit()
        conn.close()
        return self.get(approval_id)

    def purge_expired(self) -> int:
        cutoff = time.time() - _APPROVAL_TTL_SECONDS
        conn = sqlite3.connect(self.db_path)
        cur = conn.execute(
            "DELETE FROM capability_approvals WHERE created_at < ? "
            "AND status != 'pending'", (cutoff,))
        conn.commit()
        n = cur.rowcount
        conn.close()
        return n


class CapabilityDoor:
    """One dispatch door for every agent-callable capability."""

    def __init__(self, daemon: Any = None, approval_store: Any = None):
        self._daemon = daemon
        self.approvals = approval_store or CapabilityApprovalStore()
        self._audit = None

    # ── audit ────────────────────────────────────────────────────────
    def _audit_log(self, *, agent_id: str, capability_id: str,
                   status_code: int, outcome: str, duration_ms: int,
                   publisher: str) -> None:
        if self._audit is None:
            if self._daemon is not None and \
                    hasattr(self._daemon, "_get_audit_logger"):
                self._audit = self._daemon._get_audit_logger()
            else:
                try:
                    from ..audit_logger import AuditLogger

                    self._audit = AuditLogger()
                except Exception as e:
                    logger.warning("audit logger unavailable: %s", e)
                    return
        try:
            self._audit.log_action(
                actor_id=f"agent:{agent_id}",
                action="AGENT_CALL",
                resource_type="agent_capability",
                resource_id=capability_id,
                details={"outcome": outcome, "status_code": status_code,
                         "duration_ms": duration_ms,
                         "publisher": publisher},
                result="success" if status_code < 400 else "denied",
            )
        except Exception as e:
            logger.warning("capability audit write failed: %s", e)

    # ── role / authority ─────────────────────────────────────────────
    def _publisher_role(self, agent_id: str, publisher: str) -> str:
        """Publisher resolves SERVER-SIDE: the publish registry wins;
        the body's publisher field is only a fallback (§5.3/§9)."""
        effective = publisher
        try:
            reg = self._publisher_registry().get(agent_id) or {}
            if reg.get("publisher"):
                effective = str(reg["publisher"])
        except Exception:
            pass
        if not effective:
            return ""
        # #271: daemon auth ctx (jwt/tid) carries tenant roles when
        # identity is enabled; module-key callers are admin by contract.
        role = ""
        if self._daemon is not None and \
                hasattr(self._daemon, "resolve_publisher_role"):
            try:
                role = self._daemon.resolve_publisher_role(effective) or ""
            except Exception:
                role = ""
        return role or "admin"

    def _publisher_registry(self) -> dict:
        reg = getattr(self, "_pub_reg", None)
        if reg is None:
            from ..agent_platform.publish_registry import PublishRegistry

            reg = PublishRegistry()
            self._pub_reg = reg
        return reg

    # ── the door ─────────────────────────────────────────────────────
    async def call(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """Body: {capability_id, params, agent_id, version, publisher}.
        version 0/absent = draft (mutations denied). Returns
        (status_code, data)."""
        started = time.monotonic()
        capability_id = str(body.get("capability_id") or "")
        agent_id = str(body.get("agent_id") or "unknown")
        publisher = str(body.get("publisher") or "")
        try:
            version = int(body.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        params = body.get("params") or {}
        if not isinstance(params, dict):
            raise DoorError(422, "params must be an object")

        outcome, status_code, data = "error", 0, None
        cap: Optional[Dict[str, Any]] = None
        publisher_role = ""
        try:
            cap = get_capability(capability_id)
            if cap is None:
                raise DoorError(404, f"unknown capability '{capability_id}'")
            if cap.get("status") != "active":
                raise DoorError(409, (
                    f"capability disabled: {capability_id} — its upstream "
                    f"{cap.get('source')} artifact is disabled or removed"))

            # Policy compile at run time (§9): authority = publisher's
            # role ∩ capability write class.
            publisher_role = self._publisher_role(agent_id, publisher)
            if cap.get("mutation"):
                if version < 1:
                    raise DoorError(403, (
                        f"mutations denied for draft agents "
                        f"({capability_id}); publish the agent first"))
                if publisher_role not in WRITE_ROLES:
                    raise DoorError(403, (
                        f"publisher '{publisher or '?'}' "
                        f"({publisher_role or '?'}) lacks write class "
                        f"'{cap.get('write_class')}' for {capability_id}"))

            errors = validate_params(cap, params)
            if errors:
                raise DoorError(422, {"errors": errors})

            if cap.get("mutation"):
                # Uniform human gate (§5.3): agent mutations NEVER execute
                # directly — an approval record carries the full params; a
                # human decides via agent_platform.approve/reject.
                rec = self.approvals.create(
                    capability_id, agent_id, version, publisher, params,
                    requester=f"agent:{agent_id}")
                data = {"approval_id": rec["id"],
                        "status": "pending_approval"}
                outcome = "approval_requested"
                status_code = 200
            else:
                status_code, data = await self._dispatch(cap, params)
                outcome = "ok" if status_code < 400 else "dispatch_failed"
            return status_code, data
        except DoorError as e:
            status_code = e.status_code
            outcome = f"denied:{status_code}"
            data = {"error": e.detail}
            raise
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            self._audit_log(
                agent_id=agent_id, capability_id=capability_id,
                status_code=status_code, outcome=outcome,
                duration_ms=duration_ms, publisher=publisher)

    # ── dispatch by source (§5.5.3) ──────────────────────────────────
    async def _dispatch(self, cap: Dict[str, Any],
                        params: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        route = cap.get("route") or {}
        kind = route.get("kind")
        if kind == "rest":
            return await self._dispatch_rest(cap, params)
        if kind == "node":
            return await self._dispatch_node(route, params)
        if kind == "rag":
            return await self._dispatch_rag(route, params)
        if kind == "skill":
            return await self._dispatch_skill(route, params)
        if kind == "connector":
            raise DoorError(501, (
                "connector dispatch lands with the connectors console "
                "(E3, fusion-plugins-ecosystem)"))
        if kind == "mcp":
            return await self._dispatch_mcp(route, params)
        raise DoorError(500, f"unrouted capability {cap.get('id')}")

    def _gateway_base(self) -> str:
        return os.environ.get("FUSION_SHELL_GATEWAY_URL", "").strip().rstrip("/")

    def _module_key(self) -> str:
        from ..shell_registry import static_module_key

        return static_module_key()

    async def _dispatch_rest(self, cap: Dict[str, Any],
                             params: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        import httpx

        route = cap.get("route") or {}
        module = str(route.get("module") or "")
        method = str(route.get("method") or "GET").upper()
        path = str(route.get("path") or "")
        if not module or not path:
            raise DoorError(500, f"rest route missing module/path: {cap.get('id')}")
        from .capabilities import HOST_MODULE

        if module == HOST_MODULE and _self_dispatch is not None:
            # in-process: the door already authenticated the caller and
            # re-validated params, and mutations only reach dispatch after
            # approval — no loop back out through HTTP
            return await _self_dispatch(method, path, params)
        base = self._gateway_base()
        key = self._module_key()
        if base:
            url = f"{base}/shell/{module}{path}"
        else:
            # no gateway: hit the target module's local API server directly
            port = os.environ.get("FUSION_API_PORT", "11442").strip()
            url = f"http://127.0.0.1:{port}{path}"
        headers = {"Content-Type": "application/json"}
        if key:
            headers["x-api-key"] = key
        try:
            async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                if method == "GET":
                    resp = await client.get(url, params=params, headers=headers)
                else:
                    resp = await client.request(method, url, json=params,
                                                headers=headers)
        except Exception as e:
            raise DoorError(502, f"rest dispatch unreachable: "
                                 f"{type(e).__name__}: {e}")
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text[:500]}
        return resp.status_code, data

    async def _dispatch_node(self, route: Dict[str, Any],
                             params: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        rt = self._runtime()
        tool_name = str(route.get("node") or "")
        try:
            result = await rt.tools.execute(tool_name, params)
        except Exception as e:
            return 502, {"ok": False,
                         "error": f"tool '{tool_name}' failed: "
                                  f"{type(e).__name__}: {e}"}
        payload = result if not isinstance(result, str) else {"text": result}
        if not isinstance(payload, dict):
            payload = {"result": payload}
        return 200, {"ok": True, "data": payload}

    async def _dispatch_rag(self, route: Dict[str, Any],
                            params: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        kb_id = str(route.get("kb") or "")
        from server.fusion_rag_client import FusionRAGClient

        client = FusionRAGClient()
        try:
            result = await client.search(
                query=str(params.get("query") or ""),
                kb_id=kb_id,
                top_k=int(params.get("top_k") or 5),
            )
        except Exception as e:
            return 502, {"ok": False,
                         "error": f"kb '{kb_id}' search failed: "
                                  f"{type(e).__name__}: {e}"}
        chunks = [
            {"text": getattr(r, "text", ""), "doc_path": getattr(r, "doc_path", ""),
             "score": getattr(r, "score", 0.0)}
            for r in (getattr(result, "results", None) or [])
        ]
        return 200, {"ok": True, "kb": kb_id, "chunks": chunks}

    async def _dispatch_skill(self, route: Dict[str, Any],
                              params: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        name = str(route.get("skill") or "")
        from ..skills_store import SkillsStore

        store = SkillsStore()
        meta = store.load_skill(name)
        if meta is None:
            raise DoorError(404, f"skill '{name}' not installed")
        return 200, {"ok": True, "skill": name,
                     "prompt": meta.get("prompt", ""),
                     "data": {k: v for k, v in meta.items() if k != "prompt"}}

    async def _dispatch_mcp(self, route: Dict[str, Any],
                            params: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        server_id = str(route.get("server") or "")
        tool = str(route.get("tool") or "")
        try:
            from ..mcp_outbound import get_outbound_registry

            registry = get_outbound_registry()
            result = await registry.call_tool(server_id, tool, params)
            return 200, {"ok": True, "server": server_id, "tool": tool,
                         "data": result}
        except Exception as e:
            return 502, {"ok": False, "server": server_id, "tool": tool,
                         "error": f"MCP call failed: {type(e).__name__}: {e}"}

    def _runtime(self):
        if self._daemon is not None and \
                hasattr(self._daemon, "_get_runtime"):
            return self._daemon._get_runtime()
        from ..runtime import AgentRuntime

        return AgentRuntime()

    # ── approval workbench (mutation gate) ───────────────────────────
    def _check_decider(self, rec: dict, decided_by: str) -> None:
        requester = rec.get("requester") or ""
        if decided_by and decided_by == requester:
            # requester != approver — the anti-self-approval invariant.
            raise DoorError(403, "requester cannot approve their own request")
        # #446: external tasks with an assigned reviewer are decided by
        # that reviewer (or, if unassigned, any non-requester).
        assigned = rec.get("assigned_to") or ""
        if rec.get("kind") == "external" and assigned and \
                decided_by and decided_by != assigned:
            raise DoorError(403, f"approval assigned to '{assigned}', "
                                 f"not '{decided_by}'")

    async def _notify_decision(self, rec: dict, reason: str = "") -> None:
        """#446: fire approval.decided webhooks — best-effort, never
        raises into the decision path."""
        try:
            from .webhooks import get_webhook_registry

            await get_webhook_registry().fire("approval.decided", {
                "approval_id": rec.get("id"),
                "kind": rec.get("kind") or "mutation",
                "capability_id": rec.get("capability_id"),
                "agent_id": rec.get("agent_id"),
                "title": rec.get("title") or "",
                "status": rec.get("status"),
                "requester": rec.get("requester"),
                "decided_by": rec.get("decided_by"),
                "decided_at": rec.get("decided_at"),
                "params": rec.get("params") or {},
                "result": (rec.get("result") or "")[:2000],
                "reason": reason,
            })
        except Exception as e:
            logger.warning("approval webhook notify failed: %s", e)

    async def approve(self, approval_id: str, decided_by: str) -> dict:
        rec = self.approvals.get(approval_id)
        if not rec:
            raise DoorError(404, f"approval '{approval_id}' not found")
        if rec["status"] != "pending":
            raise DoorError(409, f"approval already {rec['status']}")
        self._check_decider(rec, decided_by)
        if rec.get("kind") == "external":
            # #446: external task — the decision is the whole outcome;
            # no studio capability dispatches, consumers advance their own
            # state from the webhook.
            rec = self.approvals.decide(approval_id, "approved", decided_by)
            logger.info("external approval approved: %s by=%s",
                        approval_id, decided_by)
            await self._notify_decision(rec)
            return rec
        cap = get_capability(rec["capability_id"])
        if cap is None:
            raise DoorError(404, f"capability '{rec['capability_id']}' removed")
        status_code, data = await self._dispatch(cap, rec.get("params") or {})
        rec = self.approvals.decide(
            approval_id, "approved", decided_by,
            result=json.dumps(data, ensure_ascii=False)[:2000])
        rec["dispatch_status_code"] = status_code
        logger.info("capability approval approved: %s cap=%s by=%s",
                    approval_id, rec["capability_id"], decided_by)
        await self._notify_decision(rec)
        return rec

    async def reject(self, approval_id: str, decided_by: str,
                     reason: str = "") -> dict:
        rec = self.approvals.get(approval_id)
        if not rec:
            raise DoorError(404, f"approval '{approval_id}' not found")
        if rec["status"] != "pending":
            raise DoorError(409, f"approval already {rec['status']}")
        self._check_decider(rec, decided_by)
        rec = self.approvals.decide(approval_id, "rejected", decided_by,
                                    result=reason)
        logger.info("capability approval rejected: %s by=%s",
                    approval_id, decided_by)
        await self._notify_decision(rec, reason=reason)
        return rec


# in-process dispatcher for HOST_MODULE rest routes, installed by
# api_agents_platform at registration time — (method, path, params) ->
# (status_code, data). Keeps self-hosted capability dispatch off the wire.
_self_dispatch: Optional[Callable[..., Awaitable[Tuple[int, Dict[str, Any]]]]] = None


def set_self_dispatch(fn: Callable[..., Awaitable[Tuple[int, Dict[str, Any]]]]) -> None:
    global _self_dispatch
    _self_dispatch = fn


_default_door: Optional[CapabilityDoor] = None


def get_default_door(daemon: Any = None) -> CapabilityDoor:
    global _default_door
    if _default_door is None:
        _default_door = CapabilityDoor(daemon=daemon)
    return _default_door
