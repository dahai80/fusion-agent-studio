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
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
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
        echo "running (PID $(get_pid), socket=${SOCKET})"
        exit 0
    fi
    echo "not running"
    exit 1
}

restart() {
    stop || true
    start
}

case "${1:-status}" in
    start)   start ;;
    stop)    stop ;;
    restart) restart ;;
    status)  status ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 2
        ;;
esac
