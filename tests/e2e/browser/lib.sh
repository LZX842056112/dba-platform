#!/usr/bin/env bash
# 浏览器 e2e 公共函数库（由 run_browser_e2e.sh source）。
#
# 约定：
#   * 所有 chrome-ws 调用必须在**同一进程组**内完成 —— Chrome 是命令的子进程，
#     命令结束时会被回收（实测：跨命令调用会 ECONNREFUSED 9222）。
#   * ``chrome-ws eval`` 返回的是 **JSON**（字符串带引号），断言时需去掉首尾引号。

# ── chrome-ws 定位（可用 CHROME_WS 覆盖）──────────────────────────────
: "${CHROME_WS:=C:/Users/lzx84/.workbuddy/plugins/cache/codebuddy-plugins-official/superpowers-chrome/1.6.1/skills/browsing/chrome-ws}"
: "${CHROME_EXE:=C:/Program Files/Google/Chrome/Application/chrome.exe}"
export CHROME_WS

# ── 输出 ────────────────────────────────────────────────────────────
log()  { printf '  %s\n' "$*"; }
pass() { printf '  ✅ %s\n' "$*"; }
warn() { printf '  ⚠️  %s\n' "$*"; }
fail() { printf '  ❌ %s\n' "$*" >&2; FAILED=$((FAILED + 1)); }
skip() { printf 'SKIP: %s\n' "$*"; exit 3; }

# ── 探测 ────────────────────────────────────────────────────────────
PY="${PY:-C:/Users/lzx84/.workbuddy/binaries/python/versions/3.13.12/python.exe}"

# tcp_open <host> <port>
tcp_open() {
  "$PY" -c "
import socket,sys
s=socket.socket(); s.settimeout(2)
try: s.connect((sys.argv[1],int(sys.argv[2]))); print('1')
except Exception: print('0')
finally: s.close()" "$1" "$2" 2>/dev/null | tail -1
}

# http_ok <url>
http_ok() {
  "$PY" -c "
import urllib.request,sys
try:
    r=urllib.request.urlopen(sys.argv[1], timeout=4); print('1' if r.status<500 else '0')
except Exception: print('0')" "$1" 2>/dev/null | tail -1
}

# wait_http <url> <tries>
wait_http() {
  local i=0
  while [ "$i" -lt "${2:-40}" ]; do
    [ "$(http_ok "$1")" = "1" ] && return 0
    sleep 1; i=$((i + 1))
  done
  return 1
}

# ── chrome-ws 包装 ──────────────────────────────────────────────────
# cw_eval <tab> <js> —— 返回去掉 JSON 引号的纯文本
cw_eval() {
  local raw
  raw="$("$CHROME_WS" eval "$1" "$2" 2>/dev/null | tail -1)"
  # 去掉首尾双引号（JSON 字符串）；数字/布尔原样返回
  case "$raw" in
    \"*\") raw="${raw#\"}"; raw="${raw%\"}" ;;
  esac
  printf '%s' "$raw"
}

# cw_set_input <tab> <selector> <value> —— 设置**受控**输入框的值（React 兼容）
#
# ★ 不要用 ``chrome-ws fill``：它走 ``Input.insertText``，只改 DOM 不触发 React 的
#   onChange（React 的 ``_valueTracker`` 会把这次变更视为「无变化」而跳过），
#   结果提交时读取的仍是旧 state —— 实测表现出「password 为空」的登录失败。
#   正确做法：用原型链上的 value setter 写入，再派发冒泡的 ``input`` 事件。
cw_set_input() {
  local tab=$1 sel=$2 val=$3
  "$CHROME_WS" eval "$tab" "(()=>{const el=document.querySelector('$sel');if(!el)return 'notfound';el.focus();const s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;s.call(el,'$val');el.dispatchEvent(new Event('input',{bubbles:true}));return el.value})()" 2>/dev/null | tail -1
}

# wait_eval <tab> <js> <want> [tries] —— 轮询直到等于期望（字符串比较）
wait_eval() {
  local tab=$1 js=$2 want=$3 tries=${4:-40} i=0 v
  while [ "$i" -lt "$tries" ]; do
    v="$(cw_eval "$tab" "$js")"
    [ "$v" = "$want" ] && return 0
    sleep 0.5; i=$((i + 1))
  done
  return 1
}

# wait_eval_ge <tab> <js> <min> [tries] —— 轮询直到数值 >= 下限
wait_eval_ge() {
  local tab=$1 js=$2 min=$3 tries=${4:-40} i=0 v
  while [ "$i" -lt "$tries" ]; do
    v="$(cw_eval "$tab" "$js")"
    case "$v" in ''|*[!0-9]*) ;; *) [ "$v" -ge "$min" ] && return 0 ;; esac
    sleep 0.5; i=$((i + 1))
  done
  return 1
}

# assert_eq <label> <got> <want>
assert_eq() {
  if [ "$2" = "$3" ]; then pass "$1（=$2）"; else fail "$1：期望 [$3] 实得 [$2]"; fi
}

# assert_contains <label> <haystack> <needle>
assert_contains() {
  case "$2" in
    *"$3"*) pass "$1" ;;
    *) fail "$1：[$2] 中不含 [$3]" ;;
  esac
}

# assert_ge <label> <got> <min>
assert_ge() {
  case "${2:-}" in
    ''|*[!0-9]*) fail "$1：非数字 [$2]" ;;
    *) if [ "$2" -ge "$3" ]; then pass "$1（=$2）"; else fail "$1：期望 >=$3 实得 $2"; fi ;;
  esac
  }
