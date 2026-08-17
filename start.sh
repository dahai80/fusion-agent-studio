#!/bin/bash
# fusion-agent-studio lifecycle manager (start|stop|restart|status)
# Owns /tmp/fusion-studio.sock - central JSON-RPC router for the Fusion ecosystem.
# Callers: fusion-studio UpstreamServiceManager (auto-start on launch + manual start).
# Affected API: start.sh start|stop|restart|status; status exits 0 if running, 1 if not.
# Data schemas: PID file .fusion-agent-studio.pid; logs/stdout.log + logs/stderr.log.
# User instruction: "在所有依赖的上游模块根目录创建start.sh，在fusion-studio启动时需要检测上游服务是否启动，如果没有启动，尝试调用start.sh启动上游服务，如果启动不成功，fusion-studio要展示服务不存在，或者服务启动失败等等"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV="${SCRIPT_DIR}/.venv"
SOCKET="${FUSION_STUDIO_SOCKET:-/tmp/fusion-studio.sock}"
PID_FILE="${SCRIPT_DIR}/.fusion-agent-studio.pid"
LOG_DIR="${SCRIPT_DIR}/logs"
STDOUT_LOG="${LOG_DIR}/stdout.log"
STDERR_LOG="${LOG_DIR}/stderr.log"
HEALTH_WAIT=60

log_info()  { printf "\033[0;32m[INFO]\033[0m  %s\n" "$*"; }
log_warn()  { printf "\033[0;33m[WARN]\033[0m  %s\n" "$*"; }
log_error() { printf "\033[0;31m[ERROR]\033[0m %s\n" "$*"; }

ensure_venv() {
    if [[ -f "${VENV}/bin/activate" ]]; then
        # shellcheck disable=SC1091
        source "${VENV}/bin/activate"
    else
        log_warn "no .venv found at ${VENV}, using system python3"
    fi
}

get_pid() {
    [[ -f "$PID_FILE" ]] && cat "$PID_FILE" || echo ""
}

is_running() {
    local pid
    pid=$(get_pid)
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        return 0
    fi
    pgrep -f "agent_runtime.daemon_server" >/dev/null 2>&1
}

# Health = process alive AND socket file present (socket is created by the daemon).
is_healthy() {
    is_running && [[ -S "$SOCKET" ]]
}

start() {
    if is_running; then
        log_info "agent-studio already running (PID $(get_pid))"
        exit 0
    fi
    mkdir -p "$LOG_DIR"
    ensure_venv

    # 商用默认安全策略: 注入检测开启 + L2 (注入 block, 危险操作 preview).
    # 可被环境变量显式覆盖.
    export FUSION_SAFETY_INJECTION="${FUSION_SAFETY_INJECTION:-1}"
    export FUSION_SAFETY_LEVEL="${FUSION_SAFETY_LEVEL:-L2}"
    log_info "safety: injection=${FUSION_SAFETY_INJECTION} level=${FUSION_SAFETY_LEVEL}"

    # 运维修补：清掉 shell 继承的 FUSION_MLX_API_KEY（可能过期 → daemon 调 mlx_script 401）。
    # 统一走 ~/.fusion-mlx/settings.json auth.api_key，与 mlx daemon / comfyui 同源。
    unset FUSION_MLX_API_KEY

    # Clean stale socket from a previous crash.
    rm -f "$SOCKET"

    log_info "starting agent-studio daemon (socket=${SOCKET})..."
    nohup python3 -m agent_runtime.daemon_server >> "$STDOUT_LOG" 2>> "$STDERR_LOG" &
    local pid=$!
    echo "$pid" > "$PID_FILE"
    log_info "launched (PID ${pid}), waiting for socket..."

    local i
    for i in $(seq 1 "$HEALTH_WAIT"); do
        if is_healthy; then
            log_info "agent-studio running (PID ${pid}), socket ready"
            exit 0
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            log_error "process exited prematurely. recent stderr:"
            tail -n 20 "$STDERR_LOG" 2>/dev/null || true
            rm -f "$PID_FILE"
            exit 1
        fi
        sleep 1
    done

    log_error "timeout after ${HEALTH_WAIT}s waiting for socket. recent stderr:"
    tail -n 20 "$STDERR_LOG" 2>/dev/null || true
    exit 1
}

stop() {
    local pid
    pid=$(get_pid)
    if [[ -z "$pid" ]]; then
        log_info "agent-studio not running"
        rm -f "$SOCKET"
        return 0
    fi
    log_info "stopping agent-studio (PID ${pid})..."
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
    done
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$PID_FILE"
    rm -f "$SOCKET"
    log_info "stopped"
}

status() {
    if is_healthy; then
        local pid
        pid=$(get_pid)
        if [[ -z "$pid" ]]; then
            pid=$(pgrep -f "agent_runtime.daemon_server" | head -1)
            [[ -n "$pid" ]] && echo "running (PID ${pid}, launchd-managed, socket=${SOCKET})" && exit 0
        fi
        echo "running (PID ${pid}, socket=${SOCKET})"
        exit 0
    fi
    echo "not running"
    exit 1
}

restart() {
    stop || true
    start
}

# ── launchd install/uninstall ──────────────────────────────────────
# 让 daemon 开机自启 + 崩溃/被停后自动拉起, 保证 cron 调度不依赖人手动维持进程.
# 背景: 2026-08-17 daemon 07:39 被外部关停后无人拉起 -> 全天 4 个发布 cron 错过.
_LAUNCHD_PLIST="${HOME}/Library/LaunchAgents/com.fusion-agent-studio.server.plist"
_LAUNCHD_LABEL="com.fusion-agent-studio.server"

install_launchd() {
    if [[ -f "${_LAUNCHD_PLIST}" ]]; then
        log_warn "LaunchAgent already installed at ${_LAUNCHD_PLIST}"
        log_info "Use 'start.sh uninstall-launchd' to remove first"
        exit 0
    fi

    mkdir -p "$(dirname "${_LAUNCHD_PLIST}")"
    mkdir -p "${LOG_DIR}"

    local py_bin
    if [[ -f "${VENV}/bin/python3" ]]; then
        py_bin="${VENV}/bin/python3"
    else
        py_bin="$(command -v python3)"
        log_warn "no .venv/bin/python3, using ${py_bin}"
    fi

    cat > "${_LAUNCHD_PLIST}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${_LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${py_bin}</string>
        <string>-m</string>
        <string>agent_runtime.daemon_server</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:${VENV}/bin</string>
        <key>FUSION_SAFETY_INJECTION</key>
        <string>1</string>
        <key>FUSION_SAFETY_LEVEL</key>
        <string>L2</string>
    </dict>
</dict>
</plist>
PLIST

    launchctl load "${_LAUNCHD_PLIST}" 2>/dev/null || true
    log_info "LaunchAgent installed and loaded: ${_LAUNCHD_PLIST}"
    log_info "Daemon will auto-start on login and restart on crash/stop"
    log_info "NOTE: FUSION_MLX_API_KEY intentionally NOT set in plist -> daemon uses ~/.fusion-mlx/settings.json (dahai168)"
}

uninstall_launchd() {
    if [[ ! -f "${_LAUNCHD_PLIST}" ]]; then
        log_warn "No LaunchAgent found at ${_LAUNCHD_PLIST}"
        exit 0
    fi

    launchctl unload "${_LAUNCHD_PLIST}" 2>/dev/null || true
    rm -f "${_LAUNCHD_PLIST}"
    log_info "LaunchAgent uninstalled"
}

case "${1:-status}" in
    start)   start ;;
    stop)    stop ;;
    restart) restart ;;
    status)  status ;;
    install-launchd)   install_launchd ;;
    uninstall-launchd) uninstall_launchd ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|install-launchd|uninstall-launchd}"
        exit 2
        ;;
esac
