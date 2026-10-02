"""联调验证用 SSE 抓帧器（验证工具，不属于产品代码）。

订阅 ``/api/v1/stream/runs/{trace_id}?ticket=``，把**原始帧**逐行落盘，
并输出结论：``<终态事件>|<帧数>``（超时输出 ``timeout|<帧数>``）。

★ 原始帧落盘是「seq 单调 / id 帧 / 事件序」断言的一手证据。

用法::

    python sse_capture.py --trace <id> --ticket <t> --out frames.txt [--timeout 120]

可选 ``--after <seq>`` 模拟断线重连（作为 ``last_event_id`` 查询参数）。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

TERMINAL = {"run.finished", "run.error", "run.aborted"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sse_capture")
    parser.add_argument("--trace", required=True)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--after", default=None, help="Last-Event-ID 游标（断线续传模拟）")
    args = parser.parse_args(argv)

    url = f"{args.base}/api/v1/stream/runs/{args.trace}?ticket={args.ticket}"
    if args.after:
        url += f"&last_event_id={args.after}"

    frames: list[str] = []
    event_names: list[str] = []
    terminal_hit = ""
    try:
        request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            current = ""
            for raw in response:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                frames.append(line)
                if line.startswith("event:"):
                    current = line.split(":", 1)[1].strip()
                    event_names.append(current)
                    if current in TERMINAL:
                        terminal_hit = current
                        break
    except urllib.error.HTTPError as exc:
        frames.append(f"__HTTP_ERROR__ {exc.code} {exc.read().decode('utf-8', 'replace')}")
        terminal_hit = f"http_{exc.code}"
    except Exception as exc:  # noqa: BLE001 - 探针需如实回报异常
        frames.append(f"__ERROR__ {type(exc).__name__}: {exc}")
        terminal_hit = "error"

    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write("\n".join(frames) + "\n")

    seqs = [int(line.split(":", 1)[1].strip()) for line in frames if line.startswith("id:")]
    monotonic = all(b > a for a, b in zip(seqs, seqs[1:], strict=False))
    summary = {
        "__terminal": terminal_hit or "timeout",
        "__frames": len(frames),
        "__events": event_names,
        "__seq_monotonic": monotonic,
        "__max_seq": max(seqs) if seqs else 0,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
