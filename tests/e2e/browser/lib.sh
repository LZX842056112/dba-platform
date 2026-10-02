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

# ★ Git Bash（MSYS）会把以 "/" 开头的参数改写成 Windows 路径
#   （实测：``--path /ready`` 变成 ``/Program Files/Git/ready`` → InvalidURL）。
#   联调脚本大量传 ``/api/v1/...`` 这类路径，故必须关闭参数路径转换。
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

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

# ═════════════════════════════════════════════════════════════════════
# 以下为 2026-10-03 联调轮次新增（run_all.sh / scenarios/*.sh 使用）
# ═════════════════════════════════════════════════════════════════════

# 项目虚拟环境解释器（★ 本机 uv 不在 PATH，且沙箱禁止执行 uv 托管解释器，
#   故联调统一用 .venv 解释器；需在沙箱外执行）
: "${PY_VENV:=D:/Projects/vibeC/workbuddy/dba-platform/.venv/Scripts/python.exe}"
export PY_VENV

: "${API_BASE:=http://127.0.0.1:8000}"
: "${WEB_BASE:=http://127.0.0.1:5173}"
export API_BASE WEB_BASE

: "${E2E_DIR:=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
# ★ Git Bash 的 pwd 是 POSIX 形式（/d/Projects/...），Windows 版 Python 打不开；
#   转成混合形式（D:/Projects/...）后两边都能用。
if command -v cygpath >/dev/null 2>&1; then
  E2E_DIR="$(cygpath -m "$E2E_DIR")"
fi
export E2E_DIR

# assert_not_contains <label> <haystack> <needle>
assert_not_contains() {
  case "$2" in
    *"$3"*) fail "$1：[$2] 中不应含 [$3]" ;;
    *) pass "$1" ;;
  esac
}

# assert_eq_ge <label> <got> <min> —— assert_ge 的别名（可读性）
assert_eq_ge() { assert_ge "$@"; }

# cw_raw <tab> <js> —— 原样返回 chrome-ws eval 的 JSON 文本
cw_raw() {
  "$CHROME_WS" eval "$1" "$2" 2>/dev/null | tail -1
}

# cw_count <tab> <selector> —— 元素数量
cw_count() {
  cw_eval "$1" "document.querySelectorAll('$2').length"
}

# cw_text <tab> <selector> —— 元素文本
cw_text() {
  cw_eval "$1" "(document.querySelector('$2')?.textContent ?? '')"
}

# cw_click <tab> <selector> —— 点击（走 CDP 真实输入）
cw_click() {
  "$CHROME_WS" click "$1" "$2" >/dev/null 2>&1
}

# cw_screenshot <tab> <path>
cw_screenshot() {
  "$CHROME_WS" screenshot "$1" "$2" >/dev/null 2>&1 || warn "截图失败：$2"
}

# cw_html_dump <tab> <path>
cw_html_dump() {
  "$CHROME_WS" html "$1" >"$2" 2>/dev/null || true
}

# cw_console_install <tab> —— 安装 error / unhandledrejection 收集器
cw_console_install() {
  cw_eval "$1" "(()=>{if(window.__e2eInstalled)return 'already';window.__e2eErrors=[];window.addEventListener('error',e=>window.__e2eErrors.push(String(e.message)));window.addEventListener('unhandledrejection',e=>window.__e2eErrors.push('rejection:'+String(e.reason)));window.__e2eInstalled=1;return 'ok'})()" >/dev/null 2>&1
}

# cw_console_reset <tab> —— 清空已收集错误
cw_console_reset() {
  cw_eval "$1" "(()=>{window.__e2eErrors=[];return 'ok'})()" >/dev/null 2>&1
}

# cw_console_errors <tab> —— 返回已收集错误的 JSON 数组
cw_console_errors() {
  cw_eval "$1" "JSON.stringify(window.__e2eErrors||[])"
}

# cw_nav <tab> <path> [tries] —— 整页跳转到 WEB_BASE+path（SPA 内无入口时使用）
#   跳转会重载页面 → 重新安装控制台收集器。
cw_nav() {
  local tab=$1 path=$2 tries=${3:-40} i=0
  cw_eval "$tab" "(()=>{location.href='$WEB_BASE$path';return 'ok'})()" >/dev/null 2>&1
  while [ "$i" -lt "$tries" ]; do
    [ "$(cw_eval "$tab" "location.pathname")" = "$path" ] && break
    sleep 0.5; i=$((i + 1))
  done
  sleep 2
  cw_console_install "$tab"
  [ "$(cw_eval "$tab" "location.pathname")" = "$path" ]
}

# api_call <method> <path> [json_body] [token] [out_file]
#   返回单行 JSON（{"__status":<int>,"__body":<...>}）。
api_call() {
  local method=$1 path=$2 body=${3:-} token=${4:-} out=${5:-}
  local args=(--method "$method" --path "$path")
  [ -n "$body" ] && args+=(--body "$body")
  [ -n "$token" ] && args+=(--token "$token")
  [ -n "$out" ] && args+=(--out "$out")
  "$PY_VENV" "$E2E_DIR/http_probe.py" "${args[@]}" 2>/dev/null | tail -1
}

# api_status <json> —— 取状态码
api_status() {
  printf '%s' "$1" | "$PY" -c "import sys,json; print(json.load(sys.stdin).get('__status'))" 2>/dev/null | tail -1
}

# api_field <json> <dotted.path> —— 取字段（支持 a.b.0.c）
api_field() {
  printf '%s' "$1" | "$PY" -c "
import sys, json
d = json.load(sys.stdin)
cur = d.get('__body')
for part in sys.argv[1].split('.'):
    if isinstance(cur, dict) and part in cur:
        cur = cur[part]
    elif isinstance(cur, list) and part.isdigit():
        cur = cur[int(part)]
    else:
        cur = None
        break
print('' if cur is None else (json.dumps(cur, ensure_ascii=False) if isinstance(cur, (dict, list)) else cur))
" "$2" 2>/dev/null | tail -1
}

# assert_status <label> <json> <want_status>
assert_status() {
  assert_eq "$1" "$(api_status "$2")" "$3"
}

# sse_capture <trace_id> <ticket> <out_file> [timeout_s]
#   订阅 SSE 并把原始帧落盘；stdout 打印最后一行事件名。
sse_capture() {
  "$PY_VENV" "$E2E_DIR/sse_capture.py" \
    --trace "$1" --ticket "$2" --out "$3" --timeout "${4:-120}" 2>/dev/null | tail -1
}

# assert_file_contains <label> <file> <needle>
assert_file_contains() {
  if [ ! -f "$2" ]; then fail "$1：证据文件不存在 [$2]"; return; fi
  if grep -qF -- "$3" "$2"; then pass "$1"; else fail "$1：$(basename "$2") 中不含 [$3]"; fi
}

# assert_file_not_contains <label> <file> <needle>
assert_file_not_contains() {
  if [ ! -f "$2" ]; then fail "$1：证据文件不存在 [$2]"; return; fi
  if grep -qF -- "$3" "$2"; then fail "$1：$(basename "$2") 中不应含 [$3]"; else pass "$1"; fi
}

# assert_json_field_eq <label> <json> <dotted.path> <want>
assert_json_field_eq() {
  assert_eq "$1" "$(api_field "$2" "$3")" "$4"
}
