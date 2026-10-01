#!/usr/bin/env bash
# 真实浏览器全流程联调 e2e（chrome-ws 驱动 Chrome）
#
# 覆盖：登录（含错误口令）→ 提问 → 七步流水线 → SSE 进度 → SQL 折叠 → 逐面板出图
#
# 用法：
#   bash tests/e2e/browser/run_browser_e2e.sh          # 条件不足时 SKIP（退出码 3）
#   DBA_E2E_REQUIRE=1 bash .../run_browser_e2e.sh      # 条件不足视为失败（CI 严格模式）
#
# 退出码：0 全部通过 / 1 有断言失败 / 3 环境条件不足（skip）
#
# ★ 为什么全部在一个脚本内完成：Chrome 是命令的子进程，跨命令调用会
#   「ECONNREFUSED 127.0.0.1:9222」（实测）。所以 start / 操作 / 断言必须同一进程组。

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
# shellcheck source=tests/e2e/browser/lib.sh
source "$HERE/lib.sh"

FAILED=0
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$HERE/artifacts/$STAMP"
mkdir -p "$OUT"

BACKEND_PID=""
FRONTEND_PID=""
TAB=""

cleanup() {
  [ -n "$TAB" ] && "$CHROME_WS" close "$TAB" >/dev/null 2>&1 || true
  [ -n "$BACKEND_PID" ] && kill "$BACKEND_PID" >/dev/null 2>&1 || true
  [ -n "$FRONTEND_PID" ] && kill "$FRONTEND_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

printf '\n═══ 浏览器全流程联调 e2e ═══\n产物目录：%s\n\n' "$OUT"

# ── 1. preflight ────────────────────────────────────────────────────
log "1) preflight"
for hp in 3306 27017 6379; do
  if [ "$(tcp_open 192.168.200.10 "$hp")" != "1" ]; then
    [ "${DBA_E2E_REQUIRE:-0}" = "1" ] && { fail "虚拟机 192.168.200.10:$hp 不可达"; exit 1; }
    skip "虚拟机 192.168.200.10:$hp 不可达（依赖 MySQL/Mongo/Redis）"
  fi
done
[ -f "$CHROME_WS" ] || skip "找不到 chrome-ws：$CHROME_WS"
[ -f "$CHROME_EXE" ] || skip "找不到 chrome.exe：$CHROME_EXE"
pass "虚拟机组件可达 / chrome-ws 与 chrome.exe 就位"

# ── 2. 后端 ─────────────────────────────────────────────────────────
log "2) 后端（:8000）"
if [ "$(http_ok http://127.0.0.1:8000/health)" = "1" ]; then
  log "   复用已在运行的后端"
  warn "复用实例的 LLM 模式未知：若它跑的是真实模型，V7/V10/V11/V12/V13 等确定性断言可能不稳"
  warn "  → 需要确定性回归时，请先停掉它，让本脚本自行拉起（脚本会给后端注入 DBA_LLM_USE_FAKE=true）"
else
  log "   启动 uvicorn ...（注入 DBA_LLM_USE_FAKE=true 锁定确定性）"
  # ★ 本套 e2e 断言依赖确定性输出（SQL 含 gmv_ex_tax、七步全绿等）。
  #   DBA_LLM_USE_FAKE 优先级最高，即使 .env 开了 DBA_LLM_ALLOW_DEV 也会走确定性替身。
  (cd "$ROOT" && export DBA_LLM_USE_FAKE=true && exec uv run uvicorn dba.main:app \
    --host 127.0.0.1 --port 8000 --log-level warning >"$OUT/backend.log" 2>&1) &
  BACKEND_PID=$!
fi
if ! wait_http http://127.0.0.1:8000/health 60; then
  fail "后端未就绪（日志：$OUT/backend.log）"
  exit 1
fi
READY="$("$PY" -c "
import urllib.request,json
try:
    r=json.load(urllib.request.urlopen('http://127.0.0.1:8000/ready',timeout=10))
    print(r.get('status','?'))
except Exception as e: print('err')" 2>/dev/null | tail -1)"
case "$READY" in
  ok|degraded) pass "后端就绪（/ready=$READY）" ;;
  *) fail "后端 /ready=$READY" ;;
esac

# 登录接口冒烟（同时验证 seed 口令已修好）
LOGIN_CODE="$("$PY" -c "
import urllib.request,json
req=urllib.request.Request('http://127.0.0.1:8000/api/v1/auth/login',
    data=json.dumps({'username':'admin','password':'admin123'}).encode(),
    headers={'Content-Type':'application/json'}, method='POST')
try:
    r=urllib.request.urlopen(req,timeout=15); print(r.status)
except Exception as e: print('err:'+str(e)[:40])" 2>/dev/null | tail -1)"
assert_eq "后端登录接口（admin/admin123）" "$LOGIN_CODE" "200"

# ── 3. 前端 ─────────────────────────────────────────────────────────
log "3) 前端（:5173）"
if [ "$(http_ok http://127.0.0.1:5173)" = "1" ]; then
  log "   复用已在运行的 dev server"
else
  [ -d "$ROOT/frontend/node_modules" ] || skip "frontend/node_modules 不存在（先 npm install）"
  log "   启动 vite ..."
  (cd "$ROOT/frontend" && exec npm run dev >"$OUT/frontend.log" 2>&1) &
  FRONTEND_PID=$!
fi
wait_http http://127.0.0.1:5173 60 && pass "前端就绪" || { fail "前端未就绪"; exit 1; }

# ── 4. 浏览器 ───────────────────────────────────────────────────────
log "4) 启动 Chrome（独立 profile，不影响日常浏览器）"
"$CHROME_WS" start >/dev/null 2>&1 || true
sleep 2
"$CHROME_WS" tabs >/dev/null 2>&1 || { fail "Chrome 调试端口（9222）不可用"; exit 1; }
TAB="$("$CHROME_WS" new "http://127.0.0.1:5173/login" 2>/dev/null | tail -1)"
case "$TAB" in
  ws://*) pass "新标签页：$TAB" ;;
  *) fail "创建标签页失败：$TAB"; exit 1 ;;
esac
wait_eval "$TAB" "document.querySelectorAll('.login-form').length" "1" 30 ||
  fail "登录页未渲染（.login-form 未出现）"

# ── 5. 登录 ─────────────────────────────────────────────────────────
log "5) 登录流程"
assert_eq "V1 登录页渲染（.login-form）" "$(cw_eval "$TAB" "document.querySelectorAll('.login-form').length")" "1"
assert_eq "V1 输入框齐备" "$(cw_eval "$TAB" "Array.from(document.querySelectorAll('.login-form input')).map(i=>i.name).sort().join(',')")" "password,username"

# V2: 错误口令
cw_set_input "$TAB" "input[name=password]" "wrong-password" >/dev/null 2>&1
"$CHROME_WS" click "$TAB" "button[type=submit]" >/dev/null 2>&1
if wait_eval "$TAB" "document.querySelectorAll('.hint--error').length" "1" 30; then
  assert_contains "V2 错误口令被拒（提示文案）" \
    "$(cw_eval "$TAB" "document.querySelector('.hint--error')?.textContent")" "用户名或口令错误"
else
  fail "V2 错误口令未报错（.hint--error 未出现）"
fi

# V3: 正确口令
cw_set_input "$TAB" "input[name=password]" "admin123" >/dev/null 2>&1
"$CHROME_WS" click "$TAB" "button[type=submit]" >/dev/null 2>&1
if wait_eval "$TAB" "document.querySelectorAll('.query-bar').length" "1" 40; then
  pass "V3 登录成功并跳转对话页（.query-bar 出现）"
else
  fail "V3 登录后未进入对话页（.query-bar 未出现）"
  "$CHROME_WS" screenshot "$TAB" "$OUT/fail-after-login.png" >/dev/null 2>&1 || true
fi
assert_eq "V3 token 已落 localStorage" \
  "$(cw_eval "$TAB" "Boolean(localStorage.getItem('dba_token'))")" "true"
assert_eq "V4 已离开 /login" "$(cw_eval "$TAB" "location.pathname")" "/"

# ── 6. 主链路 ───────────────────────────────────────────────────────
log "6) 主链路：提问 → 七步 → 出图"
assert_eq "V5 主页双栏标题数" \
  "$(cw_eval "$TAB" "Array.from(document.querySelectorAll('.panel-title')).length >= 2")" "true"

cw_set_input "$TAB" ".query-bar input" "近 30 天 GMV 趋势" >/dev/null 2>&1
"$CHROME_WS" click "$TAB" ".query-bar button" >/dev/null 2>&1

# V7: 七步全绿（等待 7 个 badge--done）
if wait_eval "$TAB" "document.querySelectorAll('.step-list .badge--done').length" "7" 120; then
  assert_eq "V7 七步进度全绿" "$(cw_eval "$TAB" "document.querySelectorAll('.step-list .badge--done').length")" "7"
else
  done_n="$(cw_eval "$TAB" "document.querySelectorAll('.step-list .badge--done').length")"
  total_n="$(cw_eval "$TAB" "document.querySelectorAll('.step-list li').length")"
  fail "V7 七步未全部完成（$done_n/$total_n 完成）"
  "$CHROME_WS" screenshot "$TAB" "$OUT/fail-steps.png" >/dev/null 2>&1 || true
fi

# V10: SQL 折叠区含 fact_sales
SQL_TXT="$(cw_eval "$TAB" "Array.from(document.querySelectorAll('.sql-collapse')).map(e=>e.textContent).join(' ')")"
assert_contains "V10 SQL 折叠区含 fact_sales" "$SQL_TXT" "fact_sales"
assert_contains "V10 SQL 折叠区含 gmv_ex_tax" "$SQL_TXT" "gmv_ex_tax"

# V11/V12: 大屏面板 + 结论
assert_ge "V11 大屏面板数（.panel-card）" \
  "$(cw_eval "$TAB" "document.querySelectorAll('.panel-card').length")" 1
assert_contains "V11 面板标题含 GMV" \
  "$(cw_eval "$TAB" "Array.from(document.querySelectorAll('.panel-title')).map(e=>e.textContent).join(' ')")" "GMV"
assert_contains "V12 结论解读（.bubble 有内容）" \
  "$(cw_eval "$TAB" "(document.querySelector('.bubble')?.textContent ?? '')")" "SELECT"
assert_ge "V12 结论文本长度" \
  "$(cw_eval "$TAB" "((document.querySelector('.bubble')?.textContent ?? '').trim().length)")" 1

# V13: 图表真实渲染（ECharts canvas）★ 依赖大屏 spec 落库
if wait_eval_ge "$TAB" "document.querySelectorAll('.panel-card canvas').length" 1 40; then
  assert_ge "V13 图表 canvas 渲染" "$(cw_eval "$TAB" "document.querySelectorAll('.panel-card canvas').length")" 1
else
  canvas_n="$(cw_eval "$TAB" "document.querySelectorAll('.panel-card canvas').length")"
  dim_txt="$(cw_eval "$TAB" "Array.from(document.querySelectorAll('.panel-card .dim')).map(e=>e.textContent).join(' ')")"
  fail "V13 图表未渲染（canvas=$canvas_n，占位文案=[$dim_txt]）→ 可能大屏 spec 未落库"
fi

# ── 7. 落库校验（HTTP 侧，验证 T8）───────────────────────────────────
log "7) 大屏 spec 落库校验（HTTP）"
DASH_CHECK="$("$PY" -c "
import urllib.request,json,os
B='http://127.0.0.1:8000'
def call(m,p,b=None,t=None):
    req=urllib.request.Request(B+p,method=m,data=json.dumps(b).encode() if b else None,
        headers={'Content-Type':'application/json',**({'Authorization':f'Bearer {t}'} if t else {})})
    r=urllib.request.urlopen(req,timeout=30); return json.loads(r.read().decode() or '{}')
tok=call('POST','/api/v1/auth/login',{'username':'admin','password':'admin123'})['access_token']
s=call('POST','/api/v1/chat/sessions',{'title':'e2e'},tok)
q=call('POST',f\"/api/v1/chat/sessions/{s['session_id']}/query\",{'question':'近 30 天 GMV 趋势'},tok)
import urllib.request as u
dash=None
with u.urlopen(f\"{B}/api/v1/stream/runs/{q['trace_id']}?ticket={q['stream_ticket']}\",timeout=90) as r:
    cur=None
    for raw in r:
        line=raw.decode('utf-8','ignore').rstrip()
        if line.startswith('event:'): cur=line.split(':',1)[1].strip()
        elif line.startswith('data:'):
            if cur=='dashboard.spec.ready': dash=json.loads(line[5:]).get('dashboard_id')
            if cur in ('run.finished','run.error'): break
spec=call('GET',f'/api/v1/dashboards/{dash}',t=tok)
rows=sum(len(d.get('rows') or []) for d in (spec.get('data_sources') or []))
print(f'{len(spec.get(\"panels\") or [])}|{rows}')
" 2>/dev/null | tail -1)"
DASH_PANELS="${DASH_CHECK%%|*}"
DASH_ROWS="${DASH_CHECK##*|}"
assert_ge "T8 落库后面板数" "${DASH_PANELS:-0}" 1
assert_ge "T8 落库后数据行数" "${DASH_ROWS:-0}" 1

# ── 8. 截图留证 ─────────────────────────────────────────────────────
log "8) 截图"
"$CHROME_WS" screenshot "$TAB" "$OUT/final.png" >/dev/null 2>&1 \
  && pass "截图：$OUT/final.png" || warn "截图失败"
"$CHROME_WS" html "$TAB" >"$OUT/final.html" 2>/dev/null || true

# ── 结果 ────────────────────────────────────────────────────────────
printf '\n═══ 结果 ═══\n'
if [ "$FAILED" -eq 0 ]; then
  printf '✅ 全部断言通过（产物：%s）\n\n' "$OUT"
  exit 0
fi
printf '❌ %s 项断言失败（产物：%s）\n\n' "$FAILED" "$OUT"
exit 1
