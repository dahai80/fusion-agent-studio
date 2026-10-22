"""Capability tools: spine capabilities as FAS tools (#437 / E1, plan §5).

Registers the agent's capability checklist into the FAS runtime tool
registry so the LLM tool-call loop and ad-hoc tool nodes can actually
execute them. Execution ALWAYS goes through the studio's single audited
door (agent_runtime.agent_platform.door.CapabilityDoor — in-process since
#437; the old HTTP hop to the f-ecommerce console is gone).

Naming: registry names are `bt_<agent8>_<sanitized_capability>` (FAS tool
schemas and LLM tool_calls need [a-zA-Z0-9_] names; capability ids carry
dots). The ad-hoc graph builders map node.tool_name through the same
bridge_tool_name() so graph nodes resolve to the registered tool.

Draft safety: the version stamp is 0 unless manifest.status == "published"
— the door denies mutating capabilities for version-0 callers (plan §5.3),
so a draft's ⚡ tools fail loudly at the door while reads work.

All registration is idempotent and fail-soft: a door that is unavailable
degrades to honest tool errors, never daemon crashes.
"""

from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")


async def _bridge_call(capability_id: str, params: dict, *, agent_id: str,
                       version: int, publisher: str, timeout: float) -> str:
    """Dispatch one capability through the studio's audited door
    (in-process since #437); returns a JSON string envelope (or an honest
    error string) for the LLM context."""
    from .agent_platform.door import DoorError, get_default_door

    try:
        _, data = await get_default_door().call({
            "capability_id": capability_id, "params": params,
            "agent_id": agent_id, "version": version,
            "publisher": publisher})
        return json.dumps(data, ensure_ascii=False, default=str)
    except DoorError as e:
        return json.dumps({"error": e.detail, "status_code": e.status_code})
    except Exception as e:
        # include the exception TYPE: timeout errors (TimeoutError) have an
        # empty str() — "unreachable: " with no reason was undiagnosable
        return json.dumps({"error": f"capability door failed: "
                                    f"{type(e).__name__}: {e}"})


def sane_name(capability_id: str) -> str:
    """Capability id (dotted) → FAS-safe name fragment (no dots)."""
    return _SAFE_NAME_RE.sub("_", capability_id)


def bridge_tool_name(agent_id: str, capability_id: str) -> str:
    """Deterministic registry name for one agent's one capability.
    Graph builders and registration MUST both use this."""
    return f"bt_{re.sub(r'[^a-zA-Z0-9]', '', agent_id)[:8]}_{sane_name(capability_id)}"


def _make_bridge_tool(name: str, description: str,
                      required_params: dict, runner) -> object:
    """Build a BaseTool instance whose execute() calls `runner(**kwargs)`.
    Only REQUIRED params go into the schema (FAS openai_schema marks every
    listed parameter as required); the LLM may pass exactly these.
    readonly=True at the FAS permission layer ON PURPOSE: the human gate
    for mutations is the console's approval workbench (one gate, not two);
    the bridge re-checks draft/mutation policy per call."""
    from tools.base import BaseTool

    # execute MUST be defined in the class body: ABCMeta freezes
    # __abstractmethods__ at class creation, so a post-creation attribute
    # assignment leaves the class abstract (instantiation TypeError).
    async def _execute(self, **kwargs):
        return await runner(**kwargs)

    class _BT(BaseTool):
        execute = _execute

    _BT.name = name
    _BT.description = description
    _BT.parameters = dict(required_params)
    _BT.readonly = True
    return _BT()


def _version_stamp(manifest) -> int:
    """0 for drafts (bridge denies mutations), manifest version when
    published. manifest.version_int starts at 1 even for drafts, so the
    status field decides (plan §5.3)."""
    return int(getattr(manifest, "version_int", 1) or 1) \
        if getattr(manifest, "status", "draft") == "published" else 0


def sync_agent_tools(rt, agent_id: str, manifest) -> dict:
    """(Re)register the manifest's capability tools into the runtime
    registry and remove this agent's stale ones. Idempotent."""
    registered, removed, skipped = [], [], []
    version = _version_stamp(manifest)
    publisher = getattr(manifest, "author", "") or ""
    wanted = set()
    timeout = float(os.environ.get("FUSION_ECOM_TIMEOUT", "90"))

    # capability id → manifest entry (dict tool defs carry a config)
    manifest_tools = getattr(manifest, "tools", None) or []
    by_name = {}
    for t in manifest_tools:
        if isinstance(t, dict) and t.get("name"):
            by_name[str(t["name"])] = t
        elif isinstance(t, str):
            by_name[t] = {"name": t}

    # resolve capability ids through the registry
    for tool_name, entry in by_name.items():
        config = entry.get("config") if isinstance(entry, dict) else None
        cap_id = ""
        if isinstance(config, dict) and config.get("capability_id"):
            cap_id = str(config["capability_id"])
        else:
            cap_id = tool_name  # plain capability-id entries
        if not cap_id:
            continue
        name = bridge_tool_name(agent_id, cap_id)
        wanted.add(name)
        # Schema comes embedded in the manifest (console compiled it at
        # create/update time — the manifest IS the contract). Network
        # lookup is only the fallback for older manifests.
        cap = None
        if isinstance(config, dict) and config.get("cap_params") is not None:
            cap = {"id": cap_id,
                   "title": config.get("cap_title") or cap_id,
                   "description": entry.get("description") or "",
                   "mutation": bool(config.get("cap_mutation")),
                   "params": {"type": "object",
                              "properties": config.get("cap_params") or {}}}
        else:
            cap = _lookup_capability(cap_id)
        if cap is None:
            skipped.append(f"{cap_id} (not in registry)")
            continue
        params_spec = (cap.get("params") or {}).get("properties") or {}
        # `required` may be declared schema-level (standard JSON Schema:
        # "required": ["query"]) or per-property required:true — the
        # schema-level form was missed here, so the registered tool's
        # openai schema had NO properties and the LLM could not call it.
        schema_required = (cap.get("params") or {}).get("required") or []
        required_params = {
            k: {"type": str(v.get("type", "string")),
                "description": str(v.get("description", k))}
            for k, v in params_spec.items()
            if k in schema_required or v.get("required")
        }
        desc = cap.get("description") or cap.get("title") or cap_id
        if cap.get("mutation"):
            desc += " (MUTATION — executed via human approval workflow)"

        async def _runner(_cap_id=cap_id, _required=tuple(required_params),
                          **kw):
            # small-model resilience: a weak model may satisfy the required
            # param positionally — one stray argument maps onto the sole
            # required param instead of burning a 422 round-trip. Only when
            # exactly one arg is present and it is a non-empty string.
            missing = [p for p in _required
                       if p not in kw or kw[p] in (None, "")]
            if len(missing) == 1 and len(kw) == 1:
                only = next(iter(kw))
                if isinstance(kw[only], str) and kw[only].strip():
                    kw = {missing[0]: kw[only].strip()}
            return await _bridge_call(_cap_id, kw, agent_id=agent_id,
                                      version=version, publisher=publisher,
                                      timeout=timeout)

        try:
            rt.tools.register(_make_bridge_tool(
                name, desc, required_params, _runner))
            registered.append(name)
        except Exception as e:
            skipped.append(f"{cap_id} ({e})")

    # stale tools of THIS agent (e.g. capability removed from checklist)
    try:
        existing = [t.get("name") for t in (rt.tools.list_tools() or [])
                    if isinstance(t, dict)]
    except Exception:
        existing = []
    prefix = f"bt_{re.sub(r'[^a-zA-Z0-9]', '', agent_id)[:8]}_"
    for name in existing:
        if name and name.startswith(prefix) and name not in wanted:
            try:
                rt.tools.unregister(name)
                removed.append(name)
            except Exception:
                pass

    return {"registered": registered, "removed": removed,
            "skipped": skipped, "version_stamp": version,
            "publisher": publisher}


def _lookup_capability(capability_id: str):
    """Fallback lookup for pre-P3 manifests (no embedded schema): the local
    capability spine registry — no network hop since #437."""
    from .agent_platform.capabilities import get_capability

    try:
        return get_capability(capability_id)
    except Exception as e:
        logger.warning("capability lookup failed (fail-soft): %s", e)
        return None


def ensure_daemon_tools(daemon, agent_id: str) -> dict:
    """Console-invoked sync (agent.sync_tools RPC): register the agent's
    capability tools into the daemon runtime. Fail-soft by contract."""
    try:
        from .agent_package import AgentPackage

        pkg = AgentPackage(daemon._agent_dir(agent_id))
        if not pkg.exists:
            return {"registered": [], "removed": [], "skipped":
                    ["agent not found"]}
        manifest = pkg.load_manifest()
        rt = daemon._get_runtime()
        return sync_agent_tools(rt, agent_id, manifest)
    except Exception as e:
        logger.warning("ensure_daemon_tools failed for %s: %s", agent_id, e)
        return {"registered": [], "removed": [], "skipped": [str(e)]}
