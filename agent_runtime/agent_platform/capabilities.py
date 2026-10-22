"""Agent-platform capability registry (#437 / E1, generalized §5.1/§5.5).

Port of f-ecommerce agent_platform/capabilities.py — the curated,
agent-callable façade over the host surface, e-commerce entries stripped.
This table IS the security boundary: an agent can only call what appears
here, dispatched through the single audited door (`/v1/agent-platform/call`).

Generalization vs the f-ecommerce original:
- `source=rest` routes point at module×route (module id + method + path on
  the gateway /shell/<module>/ surface) instead of console-self paths.
- `source=node` routes dispatch through the local ToolRegistry.
- `source=rag` routes dispatch through FusionRAGClient kb.search.
- `source=skill` routes dispatch through the skills store (#342).

Capability manifest fields:
  id          dotted capability id (agent-facing tool name)
  title       human label shown in the Agents tool picker
  description one-line what-it-does for the LLM
  source      rest | connector | skill | node | mcp | rag
  mutation    True -> L3-gated at run time + write_class required
  write_class existing RBAC class gating the underlying write (None for
              reads; agent authority is intersected with this at call time)
  params      JSON-schema-ish dict for door-side re-validation
  route       source-specific dispatch target

Lifecycle: `status` flips to "disabled" when an upstream artifact
(connector/skill/plugin) is disabled — callers get a precise
`capability disabled` error and dependent agents are flagged (§5.5.4).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _cap(
    id: str,
    title: str,
    description: str,
    *,
    source: str,
    mutation: bool = False,
    write_class: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    returns: str = "",
    route: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "id": id,
        "title": title,
        "description": description,
        "source": source,
        "mutation": mutation,
        "write_class": write_class,
        "params": params or {"type": "object", "properties": {}},
        "returns": returns,
        "route": route or {},
        "status": "active",
    }


def _rest(module: str, method: str, path: str) -> Dict[str, Any]:
    return {"kind": "rest", "module": module, "method": method, "path": path}


def _node(name: str) -> Dict[str, Any]:
    return {"kind": "node", "node": name}


def _str_param(desc: str, required: bool = True) -> Dict[str, Any]:
    p: Dict[str, Any] = {"type": "string", "description": desc}
    if required:
        p["required"] = True
    return p


HOST_MODULE = "fusion-agent-studio"

# ── the curated table ────────────────────────────────────────────────
# Reads mirror the shipped REST surface (api_server.py route map);
# mutations are the same endpoints the permission engine gates, re-gated
# here per agent. Extended at runtime via the add_* helpers below.
CAPABILITIES: List[Dict[str, Any]] = [
    _cap(
        "agent.list", "List agents",
        "List the agents registered on this studio (name/status/model).",
        source="rest",
        params={"type": "object", "properties": {}},
        returns="agents[] (id, name, status, model)",
        route=_rest(HOST_MODULE, "GET", "/v1/agents-platform/agents"),
    ),
    _cap(
        "graph.create", "Create workflow graph (approval-gated)",
        "Create a workflow graph on this studio. Runs through the L3 "
        "approval workbench — the agent drafts the request, a human decides.",
        source="rest", mutation=True, write_class="workflow_write",
        params={
            "type": "object",
            "properties": {
                "name": _str_param("workflow name"),
                "description": _str_param("one line what it does",
                                          required=False),
            },
        },
        returns="graph_id, status",
        route=_rest(HOST_MODULE, "POST", "/v1/agent-platform/mutations/graph.create"),
    ),
]


def active_capabilities() -> List[Dict[str, Any]]:
    return [c for c in CAPABILITIES if c.get("status") == "active"]


def get_capability(capability_id: str) -> Optional[Dict[str, Any]]:
    for c in CAPABILITIES:
        if c["id"] == capability_id:
            return c
    return None


def set_status(capability_id: str, status: str) -> bool:
    """Lifecycle coupling (§5.5.4): flip a capability's status when its
    upstream artifact is disabled/removed. Returns True if found."""
    for c in CAPABILITIES:
        if c["id"] == capability_id:
            c["status"] = status
            return True
    return False


def add_rest_capability(capability_id: str, title: str, description: str,
                        module: str, method: str, path: str, *,
                        mutation: bool = False, write_class: Optional[str] = None,
                        params: Optional[Dict[str, Any]] = None,
                        returns: str = "",
                        added_by: str = "") -> Dict[str, Any]:
    """Spine auto-listing (§5.5.2, generalized): a module×route becomes a
    capability. `rest` dispatches module+method+path through the gateway
    /shell/<module>/ proxy (module key injection server-side)."""
    cap = _cap(
        capability_id, title, description,
        source="rest", mutation=mutation, write_class=write_class,
        params=params, returns=returns, route=_rest(module, method, path),
    )
    cap["provenance"] = {"module": module, "added_by": added_by}
    CAPABILITIES.append(cap)
    return cap


def add_connector_capabilities(connector_id: str, channels: List[Dict[str, Any]],
                               installed_by: str) -> List[Dict[str, Any]]:
    """Spine auto-listing (§5.5.2): connector enablement registers its read
    channels as capabilities with provenance. Writes go through the
    reviewed-list process instead."""
    added = []
    for ch in channels:
        cap = _cap(
            f"connector.{connector_id}.{ch['id']}",
            ch.get("title", ch["id"]),
            ch.get("description", ""),
            source="connector",
            mutation=bool(ch.get("mutation")),
            write_class=ch.get("write_class"),
            params=ch.get("params") or {"type": "object",
                                        "properties": {}},
            returns=ch.get("returns", ""),
            route={"kind": "connector", "connector": connector_id,
                   "channel": ch["id"]},
        )
        cap["provenance"] = {"connector": connector_id,
                             "installed_by": installed_by}
        CAPABILITIES.append(cap)
        added.append(cap)
    return added


def add_skill_capability(name: str, doc: Dict[str, Any],
                         installed_by: str) -> Dict[str, Any]:
    """Spine auto-listing (§5.5.2): an installed skill pack becomes a
    capability (route kind=skill). Params are free-form (the pack's
    handler contract is the author's) -> `open` schema, validated as
    pass-through at the door."""
    cap = _cap(
        f"skill.{name}",
        doc.get("name") or name,
        doc.get("description") or f"Skill pack ({doc.get('type', 'node')})",
        source="skill",
        params={"type": "object", "properties": {}, "open": True},
        returns="pack handler result envelope",
        route={"kind": "skill", "skill": name},
    )
    cap["provenance"] = {"skill": name, "type": doc.get("type", "node"),
                         "installed_by": installed_by}
    CAPABILITIES.append(cap)
    return cap


def add_node_capability(plugin_name: str, node_name: str,
                        ticked_by: str) -> Optional[Dict[str, Any]]:
    """Spine candidate review (§5.5.2/§8): an admin ticks a plugin node ->
    it becomes a `node.<plugin>.<node>` capability (source: node). The
    route dispatches via the local ToolRegistry."""
    cap = _cap(
        f"node.{plugin_name}.{node_name}",
        node_name,
        f"Tool '{node_name}' from source '{plugin_name}'",
        source="node",
        params={"type": "object", "properties": {}, "open": True},
        returns="tool result envelope",
        route={"kind": "node", "node": node_name},
    )
    cap["provenance"] = {"plugin": plugin_name, "ticked_by": ticked_by}
    CAPABILITIES.append(cap)
    return cap


def add_mcp_capability(server_id: str, tool: Dict[str, Any],
                       added_by: str) -> Dict[str, Any]:
    """Spine auto-listing for a remote MCP server's tool (§8): dispatched
    as MCP tools/call to the registered server URL. Params: the MCP
    inputSchema's required properties when parseable, else open."""
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(props, dict) and props:
        params = {"type": "object", "properties": props}
    else:
        params = {"type": "object", "properties": {}, "open": True}
    cap = _cap(
        f"mcp.{server_id}.{tool.get('name', 'tool')}",
        tool.get("name", "tool"),
        str(tool.get("description") or f"MCP tool on '{server_id}'")[:300],
        source="mcp",
        params=params,
        returns="MCP tool result",
        route={"kind": "mcp", "server": server_id,
               "tool": tool.get("name", "tool")},
    )
    cap["provenance"] = {"mcp_server": server_id, "added_by": added_by}
    CAPABILITIES.append(cap)
    return cap


def add_rag_capabilities(kb_id: str, title: str, description: str,
                         wired_by: str) -> Dict[str, Any]:
    """Spine auto-listing for a fusion-rag knowledge base: one KB = one
    capability. Params are STRICT (query required, top_k bounded) — the kb
    id lives in the route and is resolved server-side, never
    agent-controllable."""
    cap = _cap(
        f"knowledge.{kb_id}.search",
        f"Search {title}",
        description or f"Hybrid retrieval over the '{kb_id}' knowledge base",
        source="rag",
        params={"type": "object", "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer"},
        }, "required": ["query"]},
        returns="list of chunks: {text, doc_path, score}",
        route={"kind": "rag", "kb": kb_id},
    )
    cap["provenance"] = {"rag_kb": kb_id, "wired_by": wired_by}
    CAPABILITIES.append(cap)
    return cap


def remove_capability(capability_id: str) -> bool:
    """Drop a capability entry entirely (skill/plugin removal — distinct
    from disable, which keeps the record for the audit story)."""
    for i, c in enumerate(CAPABILITIES):
        if c["id"] == capability_id:
            CAPABILITIES.pop(i)
            return True
    return False


def disable_source(source_kind: str, artifact_id: str) -> List[str]:
    """Disable every capability belonging to one upstream artifact and
    return the affected ids (callers flag dependent agents)."""
    affected = []
    for c in CAPABILITIES:
        if c["source"] != source_kind:
            continue
        if source_kind == "connector" and \
                c.get("route", {}).get("connector") == artifact_id:
            c["status"] = "disabled"
            affected.append(c["id"])
        elif source_kind == "skill" and \
                c.get("route", {}).get("skill") == artifact_id:
            c["status"] = "disabled"
            affected.append(c["id"])
        elif source_kind == "node" and \
                c.get("provenance", {}).get("plugin") == artifact_id:
            c["status"] = "disabled"
            affected.append(c["id"])
        elif source_kind == "rag" and \
                c.get("route", {}).get("kb") == artifact_id:
            c["status"] = "disabled"
            affected.append(c["id"])
    return affected


def validate_params(cap: Dict[str, Any],
                    params: Dict[str, Any]) -> List[str]:
    """Door-side schema re-validation (§5.2 defense in depth). Minimal,
    honest checks: required presence + basic type. Extra keys rejected to
    keep agent args from smuggling intent — except `open` schemas
    (skill/connector packs whose handler contract is the author's), which
    pass through untouched."""
    if (cap.get("params") or {}).get("open"):
        return []
    errors: List[str] = []
    schema = cap.get("params") or {}
    props = schema.get("properties") or {}
    # required can be declared either as a schema-level list (standard JSON
    # Schema: "required": ["query"]) or per-property required:true — honor both
    schema_required = schema.get("required") or []
    for name, spec in props.items():
        if name in schema_required or spec.get("required"):
            if name not in params or params[name] in (None, ""):
                errors.append(f"missing required param '{name}'")
    type_map = {"string": str, "integer": int, "number": (int, float),
                "boolean": bool}
    for name, value in params.items():
        if name not in props:
            errors.append(f"unknown param '{name}'")
            continue
        want = type_map.get(props[name].get("type"))
        if want and not isinstance(value, bool if want is bool else want):
            if want is bool:
                if not isinstance(value, bool):
                    errors.append(f"param '{name}' must be boolean")
            elif not isinstance(value, want):
                errors.append(
                    f"param '{name}' must be {props[name].get('type')}")
    return errors
