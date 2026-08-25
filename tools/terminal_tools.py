"""Terminal tool — execute shell commands on the local system."""

from __future__ import annotations

import asyncio
import logging
import os
import re

from .base import BaseTool

logger = logging.getLogger(__name__)

# 审计 A-3/E-1: 终端 sink 灾难性命令黑名单. terminal 工具本就是任意 shell 执行
# (其用途即此), 不做白名单 (会破坏向后兼容 + 用例), 但挡掉不可逆的破坏性
# 模式: rm -rf (根/家目录/环境变量展开)、mkfs (格式化)、dd 到块设备 (含裸盘
# rdisk)、shutdown/reboot/halt/poweroff/init 0/kill init、裸写盘 (> >> tee).
# env FUSION_TERMINAL_UNRESTRICTED=1 可完全放开 (如受控 CI 场景). 默认即灾难
# 防线, 每条命令留日志.
# E-1: 原黑名单子串正则漏 `rm -rf ~`/`rm -rf $HOME`/`rm -rf /Users/*`/
# `dd of=/dev/rdisk0`/`init 0`/`kill -9 1`/`osascript shut down`/`tee /dev/sda`.
# 补全这些已知绕过路径.
_CATASTROPHIC_PATTERNS = [
    # rm 带 -r (递归) 标志 + 目标是根树 (以 / 开头: / /usr /opt /var ...)
    # 或家目录展开 (~ / $HOME / $USERPROFILE) / /Users/*. 这些是递归删系统树
    # 或用户家, 不可逆. 注: 仅挡递归 rm, 单文件 rm 不挡 (rm file.txt 正常).
    re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*\s+)+(/[^\s]*|~[^\s]*|\\?\$HOME|\\?\$USERPROFILE)", re.IGNORECASE),
    re.compile(r"\bmkfs\b"),
    # dd 写块设备: 含裸盘 rdisk + 相对穿越.
    re.compile(r"\bdd\b.*\bof=/dev/(r?)(sd|nvme|disk|hd)"),
    # 裸写盘: > >> tee.
    re.compile(r"(>>?)\s*/dev/(r?)(sd|nvme|disk|hd)"),
    re.compile(r"\btee\s+/dev/(r?)(sd|nvme|disk|hd)"),
    # 停机: shutdown/reboot/halt/poweroff/init 0/kill init/osascript shut down.
    re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"),
    re.compile(r"\binit\s+0\b"),
    re.compile(r"\bkill\s+(-9\s+)?1\b"),
    re.compile(r"osascript.*shut\s*down"),
]


class TerminalTool(BaseTool):
    """Execute shell commands in a subprocess."""

    name = "terminal"
    description = "Execute a shell command and return its output"
    parameters = {
        "command": {
            "type": "string",
            "description": "Shell command to execute",
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout in seconds (default: 30)",
            "default": 30,
        },
        "workdir": {
            "type": "string",
            "description": "Working directory (default: current directory)",
            "default": "",
        },
    }

    async def execute(self, **kwargs) -> str:
        command = kwargs.get("command", "")
        timeout = int(kwargs.get("timeout", 30))
        workdir = kwargs.get("workdir", "")

        if not command:
            return "Error: command is required"

        if len(command) > 10000:
            return "Error: command too long (max 10000 characters)"

        logger.info("terminal exec workdir=%s cmd=%s", workdir or "(cwd)", command[:300])

        # 审计 A-3: 灾难性命令黑名单 (secure-by-default). 放开需显式 env.
        unrestricted = os.environ.get("FUSION_TERMINAL_UNRESTRICTED", "").strip().lower() in ("1", "true", "yes")
        if not unrestricted:
            for pat in _CATASTROPHIC_PATTERNS:
                if pat.search(command):
                    logger.warning("terminal blocked catastrophic command: %s", command[:200])
                    return (
                        "Error: command matched a catastrophic pattern (rm -rf /, mkfs, "
                        "dd to disk, shutdown, raw disk write) and is blocked by default. "
                        "Set FUSION_TERMINAL_UNRESTRICTED=1 to allow."
                    )

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir if workdir else None,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return f"Error: Command timed out after {timeout}s"

            output_parts = []
            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))
            if stderr:
                output_parts.append(f"[STDERR]\n{stderr.decode('utf-8', errors='replace')}")

            result = "".join(output_parts).strip()

            if proc.returncode != 0:
                prefix = f"Command exited with code {proc.returncode}"
                if result:
                    return f"{prefix}:\n{result}"
                return prefix

            return result if result else "Command completed (exit code 0, no output)"

        except FileNotFoundError:
            return f"Error: Command not found: {command.split()[0]}"
        except Exception as e:
            return f"Error executing command: {e}"
