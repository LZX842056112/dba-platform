"""联调验证用 HTTP 探针（验证工具，不属于产品代码）。

单次请求 → 单行 JSON：``{"__status": 200, "__body": {...}}``。
可选 ``--out`` 把原始响应体落盘作为证据。

用法::

    python http_probe.py --method GET --path /api/v1/obs/overview --token <jwt>
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="http_probe")
    parser.add_argument("--method", default="GET")
    parser.add_argument("--path", required=True)
    parser.add_argument("--body", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--header", action="append", default=[])
    parser.add_argument("--out", default=None)
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args(argv)

    url = args.base + args.path
    headers = {"Content-Type": "application/json"}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    for raw in args.header:
        if ":" in raw:
            key, value = raw.split(":", 1)
            headers[key.strip()] = value.strip()
    data = args.body.encode("utf-8") if args.body else None

    request = urllib.request.Request(url, data=data, headers=headers, method=args.method)
    status = 0
    raw_text = ""
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            status = response.status
            raw_text = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw_text = exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - 探针需如实回报任何网络异常
        status = 0
        raw_text = json.dumps({"__error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(f"HTTP {status}\n{raw_text}\n")

    try:
        body: object = json.loads(raw_text) if raw_text.strip() else None
    except json.JSONDecodeError:
        body = raw_text
    print(json.dumps({"__status": status, "__body": body}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
