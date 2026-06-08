#!/usr/bin/env bash
# ============================================================================
# deploy.sh — TradingAgents-Astock 一键部署脚本
# 用法: bash scripts/deploy.sh [命令]
# 命令: deploy (默认) | start | stop | restart | status | logs | destroy
# ============================================================================
set -euo pipefail

# ── 配置 ──────────────────────────────────────────────────────────────────────
APP_NAME="tradingagents-web"
PORT=8911
COMPOSE_FILE="docker-compose.yml"
DEPLOY_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# 颜色输出
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── 前置检查 ──────────────────────────────────────────────────────────────────
check_deps() {
    local missing=()
    for cmd in docker git; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        error "缺少必要依赖: ${missing[*]}"
        error "请先安装: apt-get install -y ${missing[*]}  (或 yum install -y ${missing[*]})"
        exit 1
    fi

    # 检查 docker compose (v2) 或 docker-compose (v1)
    if docker compose version &>/dev/null 2>&1; then
        COMPOSE_CMD="docker compose"
    elif command -v docker-compose &>/dev/null; then
        COMPOSE_CMD="docker-compose"
    else
        error "需要 Docker Compose (v1 或 v2)"
        error "安装: https://docs.docker.com/compose/install/"
        exit 1
    fi
    info "Docker Compose: $COMPOSE_CMD"
}

check_env() {
    if [[ ! -f "$DEPLOY_DIR/.env" ]]; then
        error ".env 文件不存在！"
        echo ""
        echo "  请创建 $DEPLOY_DIR/.env 并填入必要配置："
        echo ""
        cat <<'EOF'
  # ---- LLM 配置（必填，选一种）----
  DEEPSEEK_API_KEY=sk-your-deepseek-key
  # MINIMAX_API_KEY=your-minimax-key
  # OPENAI_API_KEY=sk-your-openai-key

  # ---- 数据源 API Key（推荐填写）----
  SYSTEM_TICKFLOW_API_KEY=your-tickflow-key
  SYSTEM_TUSHARE_TOKEN=your-tushare-token
EOF
        echo ""
        exit 1
    fi

    # 检查至少有一个 LLM key
    if ! grep -qE '(DEEPSEEK_API_KEY|MINIMAX_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|DASHSCOPE_API_KEY|ZHIPU_API_KEY)=[^[:space:]]' "$DEPLOY_DIR/.env" 2>/dev/null; then
        warn ".env 中未检测到有效的 LLM API Key，分析功能将无法使用"
    fi
}

# ── 部署 ─────────────────────────────────────────────────────────────────────
deploy() {
    info "开始部署 TradingAgents-Astock → 端口 $PORT"
    info "部署目录: $DEPLOY_DIR"
    cd "$DEPLOY_DIR"

    check_deps
    check_env

    # 1. 拉取最新代码（如果是 git 仓库）
    if [[ -d .git ]]; then
        info "拉取最新代码..."
        git pull origin "$(git branch --show-current 2>/dev/null || echo main)" || warn "git pull 失败，继续使用本地代码"
    fi

    # 2. 构建镜像
    info "构建 Docker 镜像（首次可能需要 5-10 分钟）..."
    $COMPOSE_CMD build --no-cache "$APP_NAME"

    # 3. 停止旧容器
    info "停止旧容器..."
    $COMPOSE_CMD down --remove-orphans 2>/dev/null || true

    # 4. 启动新容器
    info "启动容器..."
    $COMPOSE_CMD up -d "$APP_NAME"

    # 5. 等待健康检查
    info "等待服务就绪..."
    local retries=30
    while (( retries > 0 )); do
        if curl -sf "http://localhost:$PORT/_stcore/health" >/dev/null 2>&1; then
            echo ""
            info "✅ 部署成功！"
            info "   访问地址: http://<ECS-IP>:$PORT"
            info "   本机测试: curl -s http://localhost:$PORT | head -5"
            return 0
        fi
        printf "  等待中... (%ds)\r" "$((31 - retries))"
        sleep 2
        retries=$((retries - 1))
    done

    echo ""
    warn "服务未在 60 秒内就绪，请检查日志："
    warn "  $COMPOSE_CMD logs --tail=50 $APP_NAME"
    return 1
}

# ── 其他命令 ─────────────────────────────────────────────────────────────────
start() {
    cd "$DEPLOY_DIR"
    check_deps
    $COMPOSE_CMD up -d "$APP_NAME"
    info "服务已启动 → http://localhost:$PORT"
}

stop() {
    cd "$DEPLOY_DIR"
    check_deps
    $COMPOSE_CMD stop "$APP_NAME"
    info "服务已停止"
}

restart() {
    cd "$DEPLOY_DIR"
    check_deps
    $COMPOSE_CMD restart "$APP_NAME"
    info "服务已重启 → http://localhost:$PORT"
}

status() {
    cd "$DEPLOY_DIR"
    check_deps
    echo -e "${CYAN}── 容器状态 ─────────────────────────────────${NC}"
    $COMPOSE_CMD ps
    echo ""
    echo -e "${CYAN}── 健康检查 ─────────────────────────────────${NC}"
    if curl -sf "http://localhost:$PORT/_stcore/health" >/dev/null 2>&1; then
        info "服务健康 ✓"
    else
        error "服务不可达 ✗"
    fi
}

logs() {
    cd "$DEPLOY_DIR"
    check_deps
    $COMPOSE_CMD logs -f --tail=100 "$APP_NAME"
}

destroy() {
    cd "$DEPLOY_DIR"
    check_deps
    warn "将删除容器和镜像，数据卷保留"
    read -rp "确认？(y/N) " confirm
    if [[ "$confirm" =~ ^[Yy]$ ]]; then
        $COMPOSE_CMD down --rmi local --remove-orphans
        info "已销毁（数据卷保留）"
    else
        info "取消"
    fi
}

# ── 入口 ─────────────────────────────────────────────────────────────────────
case "${1:-deploy}" in
    deploy)  deploy  ;;
    start)   start   ;;
    stop)    stop    ;;
    restart) restart ;;
    status)  status  ;;
    logs)    logs    ;;
    destroy) destroy ;;
    *)
        echo "用法: bash scripts/deploy.sh [命令]"
        echo ""
        echo "命令:"
        echo "  deploy   构建并部署（默认）"
        echo "  start    启动服务"
        echo "  stop     停止服务"
        echo "  restart  重启服务"
        echo "  status   查看状态"
        echo "  logs     查看实时日志"
        echo "  destroy  删除容器和镜像（保留数据）"
        exit 1
        ;;
esac
