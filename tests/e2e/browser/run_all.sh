#!/usr/bin/env bash
# 全流程联调验证入口（2026-10-03 轮次）。
#
# 阶段：
#   P0 预检（VM 组件 / Chrome / chrome-ws / .venv）
#   P1 服务启动（后端 :8000 确定性替身 + 前端 :5173）
#   P2 接口矩阵（环境 / 鉴权 / ChatBI / 观测 / FinOps / 跨模块）
#   P3 SQL 护栏分支（脚本化 LLM，实现自愈与拒绝分支的确定性复现）
#   P4 行级权限（真实库注入 → fail-closed + 真实注入 → 清理）
#   P5 故障注入（Redis 不可用 / MySQL 不可用，隔离实例）
#   P6 真实浏览器 UI 走查（chrome-ws）
#   P7 后台作业点触发 + 安全红线评测套件
#   P8 真实模型端到端验收（可选，消耗 token）
#   P9 降级态前端表现
#
# 用法：
#   bash tests/e2e/browser/run_all.sh              # 完整跑（含真实模型）
#   DBA_E2E_SKIP_REAL=1 bash .../run_all.sh        # 跳过真实模型
#   DBA_E2E_REQUIRE=1 bash .../run_all.sh          # 条件不足视为失败（CI）
#
# 退出码：0 全部通过 / 1 有失败 / 3 环境条件不足

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

ROOT="$(cd "$HERE/../../.." && pwd)"
if command -v cygpath >/dev/null 2>&1; then
  # ★ Windows 版 Python 打不开 Git Bash 的 /d/... 形式路径
  HERE="$(cygpath -m "$HERE")"
  ROOT="$(cygpath -m "$ROOT")"
fi
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$HERE/artifacts/$STAMP"
mkdir -p "$OUT/api" "$OUT/logs"
FAILED=0

# ★ Windows 默认 GBK 编码：日志重定向时 emoji（✅）会触发 UnicodeEncodeError
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

# 行级权限用例需要直连 MySQL 做注入/清理；DSN 取自仓库 .env（不入库）
E2E_DSN="${DBA_E2E_DSN:-$(grep -E '^DBA_MYSQL_DSN=' "$ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2-)}"
export E2E_DSN DBA_E2E_DSN="$E2E_DSN"

API_PORT=8000
WEB_PORT=5173
GUARD_PORT=8010
FAULT_REDIS_PORT=8020
FAULT_MYSQL_PORT=8030

PID_BACKEND=""
PID_FRONTEND=""
PID_GUARD=""
PID_REDIS=""
PID_MYSQL=""

cleanup() {
  for pid in "$PID_BACKEND" "$PID_FRONTEND" "$PID_GUARD" "$PID_REDIS" "$PID_MYSQL"; do
    [ -n "$pid" ] && kill "$pid" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

printf '\n═══ dba-platform 全流程联调验证 ═══\n产物目录：%s\n\n' "$OUT"

# ── 工具 ────────────────────────────────────────────────────────────
stop_port() {
  local port=$1
  powershell.exe -NoProfile -Command \
    "Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id \$_.OwningProcess -Force -ErrorAction SilentlyContinue }" \
    >/dev/null 2>&1 || true
  sleep 2
}

start_backend() {  # start_backend <port> <logfile> [DBA_MODE]
  local port=$1 logfile=$2 mode=${3:-fake}
  case "$mode" in
    fake)   DBA_LLM_USE_FAKE=true ;;
    real)   DBA_LLM_USE_FAKE=false ; DBA_LLM_ALLOW_DEV=true ;;
    redis)  DBA_REDIS_DSN=redis://127.0.0.1:6399/0 ;;
  esac
  (
    cd "$ROOT"
    case "$mode" in
      fake)  export DBA_LLM_USE_FAKE=true ;;
      real)  export DBA_LLM_USE_FAKE=false DBA_LLM_ALLOW_DEV=true ;;
      redis) export DBA_REDIS_DSN=redis://127.0.0.1:6399/0 DBA_EMBEDDING_USE_FAKE=true ;;
    esac
    exec "$PY_VENV" "$HERE/e2e_backend.py" --host 127.0.0.1 --port "$port" --log-level warning
  ) >"$logfile" 2>&1 &
  echo $!
}

wait_api() {
  local base=$1 tries=${2:-60} i=0
  while [ "$i" -lt "$tries" ]; do
    [ "$(http_ok "$base/health")" = "1" ] && return 0
    sleep 1; i=$((i + 1))
  done
  return 1
}

absorb() {  # absorb <exit_code> <label> —— 汇总子脚本退出码（3=skip 不算失败）
  case "$1" in
    0) return 0 ;;
    3) warn "$2：环境不足已跳过"; return 0 ;;
    *) fail "$2 失败（退出码 $1）"; return 1 ;;
  esac
}

# ── P0 预检 ─────────────────────────────────────────────────────────
log "P0 预检"
[ -f "$CHROME_WS" ] || skip "找不到 chrome-ws"
[ -f "$CHROME_EXE" ] || skip "找不到 chrome.exe"
[ -f "$PY_VENV" ] || skip "找不到 .venv 解释器：$PY_VENV"
for hp in 3306 27017 6379; do
  if [ "$(tcp_open 192.168.200.10 "$hp")" != "1" ]; then
    [ "${DBA_E2E_REQUIRE:-0}" = "1" ] && { fail "VM 192.168.200.10:$hp 不可达"; exit 1; }
    skip "VM 192.168.200.10:$hp 不可达（依赖 MySQL/Mongo/Redis）"
  fi
done
pass "VM 组件可达 / Chrome 与 chrome-ws 就位 / .venv 就位"

# ── P1 服务 ─────────────────────────────────────────────────────────
log "P1 启动服务"
if [ "$(http_ok "http://127.0.0.1:$API_PORT/health")" = "1" ]; then
  warn "复用已在运行的后端 :$API_PORT（LLM 模式未知）"
else
  PID_BACKEND="$(start_backend "$API_PORT" "$OUT/logs/backend-fake.log" fake)"
  wait_api "http://127.0.0.1:$API_PORT" 90 || { fail "后端未就绪"; exit 1; }
  pass "后端已启动（DBA_LLM_USE_FAKE=true）"
fi
if [ "$(http_ok "http://127.0.0.1:$WEB_PORT")" = "1" ]; then
  warn "复用已在运行的前端 :$WEB_PORT"
else
  ( cd "$ROOT/frontend" && exec npm run dev >"$OUT/logs/frontend.log" 2>&1 ) &
  PID_FRONTEND=$!
  wait_http "http://127.0.0.1:$WEB_PORT" 90 || { fail "前端未就绪"; exit 1; }
  pass "前端已启动"
fi

# ── P2 接口矩阵 ─────────────────────────────────────────────────────
log "P2 接口矩阵（替身）"
MATRIX="$PY_VENV $HERE/api_matrix.py"
"$PY_VENV" "$HERE/api_matrix.py" --out "$OUT/api/main" --base "http://127.0.0.1:$API_PORT" \
  --llm fake --sections env,auth,chatbi,obs,finops,cross 2>&1 | tee "$OUT/logs/api-main.log" | tail -3
absorb "${PIPESTATUS[0]}" "P2 接口矩阵"

# ── P3+P4 护栏实例（脚本化 LLM）与行级权限 ─────────────────────────
log "P3 护栏分支实例（脚本化 LLM :$GUARD_PORT）"
(
  cd "$ROOT"
  export DBA_LLM_USE_FAKE=true DBA_EMBEDDING_USE_FAKE=true DBA_E2E_LLM_SCRIPT=bad_table
  exec "$PY_VENV" "$HERE/e2e_backend.py" --host 127.0.0.1 --port "$GUARD_PORT" --log-level warning
) >"$OUT/logs/backend-guard.log" 2>&1 &
PID_GUARD=$!
if wait_api "http://127.0.0.1:$GUARD_PORT" 90; then
  "$PY_VENV" "$HERE/api_matrix.py" --out "$OUT/api/guard" --base "http://127.0.0.1:$API_PORT" \
    --sections guard --guard-base "script=http://127.0.0.1:$GUARD_PORT" \
    2>&1 | tee "$OUT/logs/api-guard.log" | tail -3
  absorb "${PIPESTATUS[0]}" "P3 护栏分支"

  log "P4 行级权限（真实库注入 + 清理）"
  DBA_E2E_DSN="$E2E_DSN" "$PY_VENV" "$HERE/api_matrix.py" --out "$OUT/api/scope" \
    --base "http://127.0.0.1:$API_PORT" --sections scope --dsn "$E2E_DSN" \
    --guard-base "script=http://127.0.0.1:$GUARD_PORT" 2>&1 | tee "$OUT/logs/api-scope.log" | tail -3
  absorb "${PIPESTATUS[0]}" "P4 行级权限"
else
  fail "护栏实例未就绪（日志：$OUT/logs/backend-guard.log）"
fi

# ── P5 故障注入 ─────────────────────────────────────────────────────
log "P5 故障注入（隔离实例）"

# Redis 不可用
(
  cd "$ROOT"
  export DBA_REDIS_DSN=redis://127.0.0.1:6399/0 DBA_EMBEDDING_USE_FAKE=true
  exec "$PY_VENV" "$HERE/e2e_backend.py" --host 127.0.0.1 --port "$FAULT_REDIS_PORT" --log-level error
) >"$OUT/logs/backend-fault-redis.log" 2>&1 &
PID_REDIS=$!
if wait_api "http://127.0.0.1:$FAULT_REDIS_PORT" 90; then
  "$PY_VENV" "$HERE/api_matrix.py" --out "$OUT/api/fault-redis" \
    --base "http://127.0.0.1:$API_PORT" --sections fault --fault redis \
    --fault-base "http://127.0.0.1:$FAULT_REDIS_PORT" 2>&1 | tee "$OUT/logs/api-fault-redis.log" | tail -3
  absorb "${PIPESTATUS[0]}" "P5 Redis 故障分支"
else
  fail "Redis 故障实例未就绪"
fi

# MySQL 不可用（读写 + 只读 DSN 都不可达）
(
  cd "$ROOT"
  export DBA_MYSQL_DSN='mysql+asyncmy://dba_user:dba_user_pwd_2026@127.0.0.1:3399/dba'
  export DBA_MYSQL_RO_DSN='mysql+asyncmy://dba_readonly:dba_readonly_pwd_2026@127.0.0.1:3399/dba'
  export DBA_EMBEDDING_USE_FAKE=true
  exec "$PY_VENV" "$HERE/e2e_backend.py" --host 127.0.0.1 --port "$FAULT_MYSQL_PORT" --log-level error
) >"$OUT/logs/backend-fault-mysql.log" 2>&1 &
PID_MYSQL=$!
if wait_api "http://127.0.0.1:$FAULT_MYSQL_PORT" 120; then
  "$PY_VENV" "$HERE/api_matrix.py" --out "$OUT/api/fault-mysql" \
    --base "http://127.0.0.1:$API_PORT" --sections fault --fault mysql \
    --fault-base "http://127.0.0.1:$FAULT_MYSQL_PORT" 2>&1 | tee "$OUT/logs/api-fault-mysql.log" | tail -3
  absorb "${PIPESTATUS[0]}" "P5 MySQL 故障分支"
else
  fail "MySQL 故障实例未就绪"
fi

# ── P6 真实浏览器 UI 走查 ───────────────────────────────────────────
log "P6 真实浏览器 UI 走查"
bash "$HERE/scenarios/ui_walkthrough.sh" "$OUT/ui" 2>&1 | tee "$OUT/logs/ui-walkthrough.log" | tail -5
absorb "${PIPESTATUS[0]}" "P6 UI 走查"

# ── P7 后台作业 + 安全评测 ──────────────────────────────────────────
log "P7 后台作业点触发 + 安全红线评测"
(
  cd "$ROOT"
  "$PY_VENV" scripts/run_anomaly_scan_once.py --hours 168
) >"$OUT/logs/worker-jobs.log" 2>&1 && pass "worker：anomaly_scan + close_stale_runs 点触发完成" \
  || fail "worker 作业点触发失败"

(
  cd "$ROOT"
  "$PY_VENV" -m dba.cli eval --suite guard --gate evals/thresholds.yaml
) >"$OUT/logs/eval-guard.log" 2>&1
EVAL_RC=$?
if [ "$EVAL_RC" -eq 0 ] && grep -q "通过率 1.000" "$OUT/logs/eval-guard.log"; then
  pass "guard 对抗集 121/121 通过（门槛达标）"
else
  fail "guard 评测未达标（退出码 $EVAL_RC，见 logs/eval-guard.log）"
fi

# ── P8 真实模型端到端验收 ───────────────────────────────────────────
if [ "${DBA_E2E_SKIP_REAL:-0}" = "1" ]; then
  warn "P8 真实模型验收：按 DBA_E2E_SKIP_REAL=1 跳过"
else
  log "P8 真实模型端到端验收（重启后端为真实模型）"
  stop_port "$API_PORT"
  PID_BACKEND="$(start_backend "$API_PORT" "$OUT/logs/backend-real.log" real)"
  if wait_api "http://127.0.0.1:$API_PORT" 120; then
    "$PY_VENV" "$HERE/api_matrix.py" --out "$OUT/api/real" --base "http://127.0.0.1:$API_PORT" \
      --llm real --sections chatbi 2>&1 | tee "$OUT/logs/api-real.log" | tail -3
    absorb "${PIPESTATUS[0]}" "P8 真实模型验收"
  else
    fail "真实模型后端未就绪"
  fi
fi

# ── P9 降级态前端 ───────────────────────────────────────────────────
log "P9 降级态前端表现"
stop_port "$API_PORT"
PID_BACKEND="$(start_backend "$API_PORT" "$OUT/logs/backend-degraded.log" redis)"
if wait_api "http://127.0.0.1:$API_PORT" 120; then
  bash "$HERE/scenarios/degraded_ui.sh" "$OUT/ui" 2>&1 | tee "$OUT/logs/ui-degraded.log" | tail -4
  absorb "${PIPESTATUS[0]}" "P9 降级态前端"
else
  fail "降级态后端未就绪"
fi

# ── 汇总 ────────────────────────────────────────────────────────────
log "汇总结果"
"$PY_VENV" - "$OUT" <<'PY' | tee "$OUT/summary.json"
import json, pathlib, sys

out = pathlib.Path(sys.argv[1])
total = {"pass": 0, "fail": 0, "skip": 0}
details = []
for path in sorted(out.glob("api/*/api_results.json")):
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    for row in rows:
        total[row["status"]] = total.get(row["status"], 0) + 1
        if row["status"] == "fail":
            details.append({"phase": path.parent.name, **row})
print(json.dumps({"total": total, "failures": details}, ensure_ascii=False, indent=2))
PY

printf '\n═══ 结果 ═══\n'
if [ "$FAILED" -eq 0 ]; then
  printf '✅ 全流程联调验证通过（产物：%s）\n\n' "$OUT"
  exit 0
fi
printf '❌ %s 项断言失败（产物：%s）\n\n' "$FAILED" "$OUT"
exit 1
