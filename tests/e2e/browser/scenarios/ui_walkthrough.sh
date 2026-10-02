#!/usr/bin/env bash
# 真实浏览器 UI 走查（chrome-ws 驱动本机 Chrome）：登录 → 提问出图 → 观测 5 页 → FinOps 4 页 → 返回对话。
#
# 覆盖：ChatBI 主链路 UI、观测/FinOps 页面渲染与真实点按（异常确认/护栏策略切换）、
#       以及上一轮 P0「观测页崩溃导致全站死锁」的回归（导航链接数必须始终为 3）。
#
# 用法：bash scenarios/ui_walkthrough.sh <产物目录>
# 退出码：0 全通过 / 1 有断言失败 / 3 环境不足
#
# ★ chrome-ws 必须与操作在同一进程组内（Chrome 是命令的子进程），故全部步骤写在本脚本里。

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib.sh
source "$HERE/../lib.sh"

OUT="${1:?用法: ui_walkthrough.sh <产物目录>}"
FAILED=0
mkdir -p "$OUT"

TAB=""
cleanup() { [ -n "$TAB" ] && "$CHROME_WS" close "$TAB" >/dev/null 2>&1 || true; }
trap cleanup EXIT

printf '\n═══ 真实浏览器 UI 走查 ═══\n产物：%s\n\n' "$OUT"

# ── 0. preflight ────────────────────────────────────────────────────
[ -f "$CHROME_WS" ] || skip "找不到 chrome-ws：$CHROME_WS"
[ -f "$CHROME_EXE" ] || skip "找不到 chrome.exe：$CHROME_EXE"
[ "$(http_ok "$WEB_BASE")" = "1" ] || skip "前端不可达：$WEB_BASE"
[ "$(http_ok "$API_BASE/health")" = "1" ] || skip "后端不可达：$API_BASE"

"$CHROME_WS" start >/dev/null 2>&1 || true
sleep 2
"$CHROME_WS" tabs >/dev/null 2>&1 || { fail "Chrome 调试端口（9222）不可用"; exit 1; }
TAB="$("$CHROME_WS" new "$WEB_BASE/login" 2>/dev/null | tail -1)"
case "$TAB" in
  ws://*) pass "新标签页已创建" ;;
  *) fail "创建标签页失败：$TAB"; exit 1 ;;
esac
wait_eval "$TAB" "document.querySelectorAll('.login-form').length" "1" 40 ||
  fail "登录页未渲染（.login-form 未出现）"
cw_console_install "$TAB"

# ── 1. 登录页 ───────────────────────────────────────────────────────
log "1) 登录页"
assert_eq "U1 登录页渲染（.login-form）" "$(cw_count "$TAB" ".login-form")" "1"
assert_eq "U1b 输入框齐备" \
  "$(cw_eval "$TAB" "Array.from(document.querySelectorAll('.login-form input')).map(i=>i.name).sort().join(',')")" \
  "password,username"

cw_set_input "$TAB" "input[name=password]" "wrong-password" >/dev/null 2>&1
cw_click "$TAB" "button[type=submit]"
if wait_eval "$TAB" "document.querySelectorAll('.hint--error').length" "1" 30; then
  assert_contains "U2 错误口令被拒（提示文案）" \
    "$(cw_text "$TAB" ".hint--error")" "用户名或口令错误"
else
  fail "U2 错误口令未报错"
fi

cw_set_input "$TAB" "input[name=password]" "admin123" >/dev/null 2>&1
cw_click "$TAB" "button[type=submit]"
if wait_eval "$TAB" "document.querySelectorAll('.query-bar').length" "1" 40; then
  pass "U3 登录成功并进入对话页"
else
  fail "U3 登录后未进入对话页"
  cw_screenshot "$TAB" "$OUT/fail-after-login.png"
fi
assert_eq "U3b token 落 localStorage" "$(cw_eval "$TAB" "Boolean(localStorage.getItem('dba_token'))")" "true"
assert_eq "U4 顶部导航链接数" "$(cw_count "$TAB" "a.nav-link")" "3"

# ── 2. 提问 → 七步 → 出图 ───────────────────────────────────────────
log "2) 主链路：提问 → 七步 → 出图"
cw_set_input "$TAB" ".query-bar input" "近 30 天 GMV 趋势" >/dev/null 2>&1
cw_click "$TAB" ".query-bar button"

if wait_eval "$TAB" "document.querySelectorAll('.step-list .badge--done').length" "7" 150; then
  assert_eq "U5 七步进度全绿" "$(cw_count "$TAB" ".step-list .badge--done")" "7"
else
  done_n="$(cw_count "$TAB" ".step-list .badge--done")"
  total_n="$(cw_count "$TAB" ".step-list li")"
  fail "U5 七步未全部完成（$done_n/$total_n）"
  cw_screenshot "$TAB" "$OUT/fail-steps.png"
fi
# 回归：终态后必须能再次提问（输入非空时按钮可用）——上一轮 P0 的表现是永久 disabled
cw_set_input "$TAB" ".query-bar input" "再问一次" >/dev/null 2>&1
assert_eq "U5b 终态后可再次提问（按钮未 disabled）" \
  "$(cw_eval "$TAB" "Boolean(document.querySelector('.query-bar button')?.disabled)")" "false"
cw_set_input "$TAB" ".query-bar input" "" >/dev/null 2>&1
assert_contains "U6 SQL 折叠区含 fact_sales" \
  "$(cw_eval "$TAB" "Array.from(document.querySelectorAll('.sql-collapse')).map(e=>e.textContent).join(' ')")" \
  "fact_sales"
assert_eq_ge "U7 大屏面板数" "$(cw_count "$TAB" ".panel-card")" 1
if wait_eval_ge "$TAB" "document.querySelectorAll('.panel-card canvas').length" 1 60; then
  assert_eq_ge "U7b 图表 canvas 渲染" "$(cw_count "$TAB" ".panel-card canvas")" 1
else
  fail "U7b 图表未渲染（canvas=0）"
fi
assert_eq_ge "U8 结论文本长度" \
  "$(cw_eval "$TAB" "((document.querySelector('.bubble')?.textContent ?? '').trim().length)")" 1
cw_screenshot "$TAB" "$OUT/01-chatbi-dashboard.png"

# ── 3. 观测 5 页 ────────────────────────────────────────────────────
log "3) 观测页（回归：不得崩溃 / 不得全站死锁）"
cw_click "$TAB" "a.nav-link[href='/observability']"
wait_eval "$TAB" "location.pathname" "/observability" 40 || fail "U9 未跳转到 /observability"
sleep 2
assert_eq "U9b 观测总览 KPI 卡数" "$(cw_count "$TAB" ".metric-value")" "4"
assert_eq "U9c 崩溃回归：导航链接数仍为 3" "$(cw_count "$TAB" "a.nav-link")" "3"
assert_eq_ge "U9d 页面非空白（main 文本长度）" \
  "$(cw_eval "$TAB" "((document.querySelector('main')?.innerText ?? '').trim().length)")" 20
assert_eq "U9e 观测子页面无导航入口（可用性观察：仅能通过 URL 直达）" \
  "$(cw_eval "$TAB" "document.querySelectorAll('a[href^=\"/observability/\"]').length")" "0"
cw_screenshot "$TAB" "$OUT/02-observability-overview.png"

if cw_nav "$TAB" "/observability/runs"; then pass "U10 直达 /observability/runs"; else fail "U10 未到达 runs"; fi
assert_eq_ge "U10b Runs 列表行数" "$(cw_count "$TAB" "table.data-table tbody tr")" 1
cw_click "$TAB" "table.data-table tbody tr:first-child a"
if wait_eval "$TAB" "location.pathname.startsWith('/observability/runs/')" "true" 40; then
  pass "U10c 点击「详情」跳转 Run 详情页"
else
  fail "U10c 详情跳转失败"
fi
sleep 2
assert_eq "U10d 死锁回归：详情页导航仍在" "$(cw_count "$TAB" "a.nav-link")" "3"
assert_eq_ge "U10e Run 详情页正文非空" \
  "$(cw_eval "$TAB" "((document.querySelector('main')?.innerText ?? '').trim().length)")" 10
cw_screenshot "$TAB" "$OUT/03-observability-run-detail.png"

if cw_nav "$TAB" "/observability/anomalies"; then pass "U11 直达 /observability/anomalies"; else fail "U11 未到达 anomalies"; fi
if [ "$(cw_count "$TAB" "table.data-table tbody tr")" != "0" ] &&
   [ "$(cw_count "$TAB" "table.data-table tbody tr")" != "" ]; then
  cw_console_reset "$TAB"
  cw_click "$TAB" "table.data-table tbody tr:first-child button"
  sleep 2
  assert_eq "U11b 异常「确认」后无前端异常" "$(cw_console_errors "$TAB")" "[]"
else
  pass "U11b 异常页空态（暂无异常，跳过点按）"
fi
cw_screenshot "$TAB" "$OUT/04-observability-anomalies.png"

if cw_nav "$TAB" "/observability/topology"; then pass "U12 直达 /observability/topology"; else fail "U12 未到达 topology"; fi
assert_eq_ge "U12b 拓扑页正文非空" \
  "$(cw_eval "$TAB" "((document.querySelector('main')?.innerText ?? '').trim().length)")" 6

# ── 4. FinOps 4 页 ──────────────────────────────────────────────────
log "4) FinOps 页"
cw_click "$TAB" "a.nav-link[href='/finops']"
wait_eval "$TAB" "location.pathname" "/finops" 40 || fail "U13 未跳转到 /finops"
sleep 2
assert_eq "U13b 成本总览 KPI 卡数" "$(cw_count "$TAB" ".metric-value")" "3"
cw_screenshot "$TAB" "$OUT/05-finops-overview.png"

if cw_nav "$TAB" "/finops/budgets"; then pass "U14 直达 /finops/budgets"; else fail "U14 未到达 budgets"; fi
assert_eq_ge "U14b 护栏策略开关数" "$(cw_count "$TAB" "input[type=checkbox]")" 1
cw_console_reset "$TAB"
cw_click "$TAB" "input[type=checkbox]"
sleep 2
assert_eq "U14c 真实点击护栏开关无前端异常" "$(cw_console_errors "$TAB")" "[]"
cw_screenshot "$TAB" "$OUT/06-finops-budgets.png"

if cw_nav "$TAB" "/finops/reuse"; then pass "U15 直达 /finops/reuse"; else fail "U15 未到达 reuse"; fi
assert_eq_ge "U15b 复用率页正文非空" \
  "$(cw_eval "$TAB" "((document.querySelector('main')?.innerText ?? '').trim().length)")" 6

if cw_nav "$TAB" "/finops/recommendations"; then pass "U16 直达 /finops/recommendations"; else fail "U16 未到达推荐页"; fi
assert_eq_ge "U16b 优化建议页正文非空" \
  "$(cw_eval "$TAB" "((document.querySelector('main')?.innerText ?? '').trim().length)")" 6
cw_screenshot "$TAB" "$OUT/07-finops-recommendations.png"

# ── 5. 回到对话页（全站可用性回归） ─────────────────────────────────
log "5) 回到对话页（全站可用性）"
cw_click "$TAB" "a.nav-link[href='/']"
if wait_eval "$TAB" "location.pathname" "/" 40; then
  pass "U17 返回对话页成功"
else
  fail "U17 返回对话页失败"
fi
cw_console_install "$TAB"
assert_eq "U17b 对话页仍可交互（输入框存在）" "$(cw_count "$TAB" ".query-bar input")" "1"
assert_eq "U17c 导航链接数" "$(cw_count "$TAB" "a.nav-link")" "3"
assert_eq "U18 全流程控制台零异常" "$(cw_console_errors "$TAB")" "[]"
cw_screenshot "$TAB" "$OUT/08-back-to-chat.png"
cw_html_dump "$TAB" "$OUT/final.html"

printf '\n═══ UI 走查结果：%s 项失败 ═══\n' "$FAILED"
[ "$FAILED" -eq 0 ] && exit 0 || exit 1
