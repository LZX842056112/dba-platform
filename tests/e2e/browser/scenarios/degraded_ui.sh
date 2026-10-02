#!/usr/bin/env bash
# 依赖降级时的前端表现（真实浏览器）：后端故障（Redis 不可用 → 业务接口 500）时，
# 页面必须「有提示、不白屏、导航仍在」——而不是整站卸载或白屏。
#
# 前置：调用方需把 :8000 指向故障实例（见 run_all.sh 的故障阶段）。
# 用法：bash scenarios/degraded_ui.sh <产物目录>
# 退出码：0 通过 / 1 失败 / 3 环境不足

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib.sh
source "$HERE/../lib.sh"

OUT="${1:?用法: degraded_ui.sh <产物目录>}"
FAILED=0
mkdir -p "$OUT"

TAB=""
cleanup() { [ -n "$TAB" ] && "$CHROME_WS" close "$TAB" >/dev/null 2>&1 || true; }
trap cleanup EXIT

printf '\n═══ 降级态前端表现走查 ═══\n'

[ -f "$CHROME_WS" ] || skip "找不到 chrome-ws"
[ "$(http_ok "$WEB_BASE")" = "1" ] || skip "前端不可达：$WEB_BASE"

# 前置校验：后端确实处于降级态（否则本场景无意义）
READY="$("$PY_VENV" "$E2E_DIR/http_probe.py" --method GET --path /ready 2>/dev/null | tail -1)"
REDIS_OK="$(api_field "$READY" "components.redis.ok")"
if [ "$REDIS_OK" != "False" ] && [ "$REDIS_OK" != "false" ]; then
  skip "后端当前 Redis 正常（需先把 :8000 指向 Redis 不可用实例）"
fi
pass "前置：后端 /ready 显示 redis 不可用（降级态）"

"$CHROME_WS" start >/dev/null 2>&1 || true
sleep 2
TAB="$("$CHROME_WS" new "$WEB_BASE/login" 2>/dev/null | tail -1)"
case "$TAB" in ws://*) ;; *) fail "创建标签页失败"; exit 1 ;; esac
wait_eval "$TAB" "document.querySelectorAll('.login-form').length" "1" 40 ||
  fail "登录页未渲染"
cw_console_install "$TAB"
cw_eval "$TAB" "(()=>{localStorage.clear();return 'ok'})()" >/dev/null 2>&1

assert_eq_ge "R1 降级态登录页仍渲染（非白屏）" \
  "$(cw_eval "$TAB" "((document.querySelector('main')?.innerText ?? '').trim().length)")" 10
assert_eq "R2 降级态导航栏仍在（3 个链接）" "$(cw_count "$TAB" "a.nav-link")" "3"

log "尝试登录（后端降级 → 预期失败但必须有提示）"
cw_set_input "$TAB" "input[name=password]" "admin123" >/dev/null 2>&1
cw_click "$TAB" "button[type=submit]"
if wait_eval "$TAB" "document.querySelectorAll('.hint--error').length" "1" 40; then
  pass "R3 登录失败有可见提示：$(cw_text "$TAB" ".hint--error")"
else
  fail "R3 登录失败无任何提示"
fi
assert_eq_ge "R4 提示后页面未白屏" \
  "$(cw_eval "$TAB" "((document.querySelector('main')?.innerText ?? '').trim().length)")" 10
assert_eq "R5 提示后导航仍可用（未整站死锁）" "$(cw_count "$TAB" "a.nav-link")" "3"
assert_eq "R6 未卸载登录表单" "$(cw_count "$TAB" ".login-form")" "1"
cw_screenshot "$TAB" "$OUT/09-degraded-login.png"

printf '\n═══ 降级态走查结果：%s 项失败 ═══\n' "$FAILED"
[ "$FAILED" -eq 0 ] && exit 0 || exit 1
