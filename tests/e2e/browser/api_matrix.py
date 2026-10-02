"""联调验证：接口/分支矩阵执行器（验证工具，不属于产品代码）。

对齐《联调验证报告-2026-10-03》覆盖矩阵：环境/鉴权、ChatBI 主链路与分支、
Observability、FinOps、跨模块数据交互、行级权限、故障注入。

产物：
  * ``<out>/api_results.json``   结构化结果（id / 模块 / 名称 / 结论 / 详情）
  * ``<out>/evidence/*.json``    关键接口原始响应（证据）
  * stdout 末行 = 汇总 JSON

用法::

    python api_matrix.py --out <dir> [--llm fake|real] [--guard-base mode=URL]
                         [--scope-rule] [--fault redis|mysql] [--sections env,auth,...]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

TERMINAL = {"run.finished", "run.error", "run.aborted"}


@dataclass
class Matrix:
    base: str
    out_dir: str
    llm: str = "fake"
    results: list[dict[str, Any]] = field(default_factory=list)
    tokens: dict[str, str] = field(default_factory=dict)

    def check(self, cid: str, name: str, module: str, status: str, detail: str = "") -> None:
        self.results.append(
            {"id": cid, "name": name, "module": module, "status": status, "detail": detail}
        )
        mark = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[status]
        print(f"  [{mark}] {cid} {name}" + (f" — {detail}" if detail else ""))

    def expect(self, cid: str, name: str, module: str, cond: bool, detail: str = "") -> None:
        self.check(cid, name, module, "pass" if cond else "fail", detail)

    def http(
        self,
        method: str,
        path: str,
        body: Any = None,
        token: str | None = None,
        headers: dict[str, str] | None = None,
        base: str | None = None,
        timeout: float = 90.0,
    ) -> tuple[int, Any]:
        url = (base or self.base) + path
        payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json"}
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        hdrs.update(headers or {})
        request = urllib.request.Request(url, data=payload, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.status
                text = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            text = exc.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - 探针如实回报网络异常
            return 0, {"__error": f"{type(exc).__name__}: {exc}"}
        try:
            return status, json.loads(text) if text.strip() else None
        except json.JSONDecodeError:
            return status, text

    def evidence(self, name: str, data: Any) -> None:
        with open(f"{self.out_dir}/evidence/{name}.json", "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)

    def sse(
        self, trace: str, ticket: str, name: str, timeout: float = 180.0, base: str | None = None
    ) -> dict[str, Any]:
        url = f"{base or self.base}/api/v1/stream/runs/{trace}?ticket={ticket}"
        frames: list[str] = []
        events: list[str] = []
        seqs: list[int] = []
        terminal = ""
        try:
            request = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                for raw in response:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    frames.append(line)
                    if line.startswith("id:"):
                        try:
                            seqs.append(int(line.split(":", 1)[1].strip()))
                        except ValueError:
                            pass
                    elif line.startswith("event:"):
                        current = line.split(":", 1)[1].strip()
                        events.append(current)
                        if current in TERMINAL:
                            terminal = current
                            break
        except urllib.error.HTTPError as exc:
            frames.append(f"__HTTP_ERROR__ {exc.code} {exc.read().decode('utf-8', 'replace')}")
            terminal = f"http_{exc.code}"
        except Exception as exc:  # noqa: BLE001
            frames.append(f"__ERROR__ {type(exc).__name__}: {exc}")
            terminal = "error"
        with open(f"{self.out_dir}/evidence/sse_{name}.txt", "w", encoding="utf-8") as handle:
            handle.write("\n".join(frames) + "\n")
        return {
            "terminal": terminal,
            "frames": len(frames),
            "events": events,
            "seq_monotonic": all(b > a for a, b in zip(seqs, seqs[1:], strict=False)),
            "max_seq": max(seqs) if seqs else 0,
            "raw": "\n".join(frames),
        }

    def login(self, username: str, password: str) -> tuple[int, Any]:
        return self.http("POST", "/api/v1/auth/login", {"username": username, "password": password})

    def start_run(
        self,
        token: str,
        question: str,
        options: dict[str, Any] | None = None,
        title: str = "e2e",
        base: str | None = None,
    ) -> dict[str, Any]:
        status, session = self.http(
            "POST", "/api/v1/chat/sessions", {"title": title}, token, base=base
        )
        if status != 200 or not isinstance(session, dict):
            return {"error": f"session create failed: {status} {session}"}
        sid = session.get("session_id")
        payload: dict[str, Any] = {"question": question}
        if options:
            payload["options"] = options
        status, started = self.http(
            "POST", f"/api/v1/chat/sessions/{sid}/query", payload, token, base=base
        )
        if status != 200 or not isinstance(started, dict):
            return {"error": f"query start failed: {status} {started}", "session_id": sid}
        return {
            "session_id": sid,
            "trace_id": started.get("trace_id"),
            "ticket": started.get("stream_ticket"),
        }


def section_env(mx: Matrix) -> None:
    status, body = mx.http("GET", "/health")
    mx.expect(
        "A1", "/health 存活探针", "环境", status == 200 and body == {"status": "ok"}, f"{status}"
    )

    status, body = mx.http("GET", "/ready")
    comps = (body or {}).get("components", {}) if isinstance(body, dict) else {}
    state = (body or {}).get("status") if isinstance(body, dict) else None
    mx.expect(
        "A2",
        "/ready 依赖自检（组件逐项 + 降级语义）",
        "环境",
        status == 200 and state in {"ok", "degraded"} and bool(comps),
        f"status={state} components={sorted(comps)}",
    )
    mx.expect(
        "A2b",
        "/ready 暴露 metering_bound",
        "环境",
        isinstance(body, dict) and "metering_bound" in body,
        str((body or {}).get("metering_bound")),
    )
    mx.evidence("ready", body)

    status, body = mx.http("GET", "/metrics")
    text = body if isinstance(body, str) else json.dumps(body)
    mx.expect(
        "A3",
        "/metrics 暴露埋点关键指标",
        "环境",
        status == 200 and "dba_metering_bound" in text and "telemetry_queue_depth" in text,
        f"len={len(text)}",
    )


def section_auth(mx: Matrix) -> None:
    status, body = mx.login("admin", "admin123")
    ok = status == 200 and isinstance(body, dict) and bool(body.get("access_token"))
    mx.expect("B1", "登录成功（admin）", "鉴权", ok, f"{status}")
    if ok:
        mx.tokens["admin"] = body["access_token"]
        mx.tokens["admin_refresh"] = body.get("refresh_token", "")
        mx.evidence("login_admin", body)

    status, body = mx.login("analyst1", "analyst123")
    ok = status == 200 and isinstance(body, dict) and bool(body.get("access_token"))
    mx.expect("B2", "登录成功（analyst1）", "鉴权", ok, f"{status}")
    if ok:
        mx.tokens["analyst"] = body["access_token"]

    status, _ = mx.login("admin", "wrong-password")
    mx.expect("B3", "口令错误 → 401", "鉴权", status == 401, f"{status}")
    status, _ = mx.login("no_such_user_e2e", "x")
    mx.expect("B4", "用户不存在 → 401（不泄露存在性）", "鉴权", status == 401, f"{status}")
    status, _ = mx.http("POST", "/api/v1/auth/login", {"username": "", "password": "x"})
    mx.expect("B5", "空用户名 → 401", "鉴权", status == 401, f"{status}")

    status, body = mx.http(
        "POST", "/api/v1/auth/refresh", {"refresh_token": mx.tokens.get("admin_refresh", "")}
    )
    mx.expect(
        "B6",
        "refresh 换取新 access token",
        "鉴权",
        status == 200 and isinstance(body, dict) and bool(body.get("access_token")),
        f"{status}",
    )
    status, _ = mx.http("POST", "/api/v1/auth/refresh", {"refresh_token": "not-a-jwt"})
    mx.expect("B7", "非法 refresh token → 401", "鉴权", status == 401, f"{status}")

    admin = mx.tokens.get("admin", "")
    analyst = mx.tokens.get("analyst", "")
    status, body = mx.http("GET", "/api/v1/auth/me", token=admin)
    me = body if isinstance(body, dict) else {}
    mx.expect(
        "B8",
        "/auth/me 返回 user/roles/scope_summary",
        "鉴权",
        status == 200 and "user" in me,
        f"{status}",
    )
    mx.evidence("auth_me", body)
    username = (me.get("user") or {}).get("username")
    mx.check(
        "B8b",
        "/auth/me username 语义（问题 5 复验）",
        "鉴权",
        "pass" if username == "admin" else "fail",
        f"username={username!r}（期望 'admin'）",
    )

    status, _ = mx.http("POST", "/api/v1/auth/logout", {}, token=admin)
    mx.expect("B9", "logout 幂等返回 ok", "鉴权", status == 200, f"{status}")
    status, _ = mx.http("GET", "/api/v1/auth/users", token=admin)
    mx.expect("B10", "users 列表（admin）→ 200", "鉴权", status == 200, f"{status}")
    status, _ = mx.http("GET", "/api/v1/auth/users", token=analyst)
    mx.expect("B11", "users 列表（analyst）→ 403 越权", "鉴权", status == 403, f"{status}")
    status, _ = mx.http("POST", "/api/v1/auth/users/2/roles", {"role_ids": [1]}, token=analyst)
    mx.expect("B12", "角色分配（analyst）→ 403 越权", "鉴权", status == 403, f"{status}")
    status, _ = mx.http("GET", "/api/v1/chat/sessions")
    mx.expect("B13", "无 Authorization → 401", "鉴权", status == 401, f"{status}")
    status, _ = mx.http("GET", "/api/v1/chat/sessions", token="a.b.c")
    mx.expect("B14", "非法签名 token → 401", "鉴权", status == 401, f"{status}")
    status, _ = mx.http("GET", "/api/v1/obs/runs", token=admin)
    mx.expect("B15", "合法 token 可访问受保护接口", "鉴权", status == 200, f"{status}")


def section_chatbi(mx: Matrix) -> None:
    token = mx.tokens.get("admin", "")
    analyst = mx.tokens.get("analyst", "")

    status, session = mx.http("POST", "/api/v1/chat/sessions", {"title": "e2e-session"}, token)
    sid = (session or {}).get("session_id") if isinstance(session, dict) else None
    mx.expect("C1", "创建会话", "ChatBI", status == 200 and bool(sid), f"{status}")
    status, sessions = mx.http("GET", "/api/v1/chat/sessions", token=token)
    mx.expect(
        "C1b", "会话列表", "ChatBI", status == 200 and isinstance(sessions, list), f"{status}"
    )
    if isinstance(sessions, list) and sessions:
        leaked = any("_id" in row for row in sessions if isinstance(row, dict))
        mx.check(
            "C1c",
            "会话列表不泄露 Mongo 内部 _id（问题 6 复验）",
            "ChatBI",
            "fail" if leaked else "pass",
            "响应含 _id" if leaked else "无 _id",
        )
    if sid:
        status, messages = mx.http("GET", f"/api/v1/chat/sessions/{sid}/messages", token=token)
        mx.expect("C1d", "会话消息列表", "ChatBI", status == 200, f"{status}")
        status, other = mx.http("GET", f"/api/v1/chat/sessions/{sid}/messages", token=analyst)
        mx.expect(
            "C2",
            "会话归属隔离（他人会话返回空）",
            "ChatBI",
            status == 200 and other == [],
            f"{status} {other!r}"[:100],
        )
        status, _ = mx.http(
            "POST", f"/api/v1/chat/sessions/{sid}/query", {"question": "   "}, token
        )
        mx.expect("C3", "空问题 → 400（带错误码）", "ChatBI", status == 400, f"{status}")

    run = mx.start_run(token, "近 30 天 GMV 趋势")
    if "error" in run:
        mx.check("C4", "主链路 Run 启动", "ChatBI", "fail", run["error"])
    else:
        trace = run["trace_id"]
        sse = mx.sse(trace, run["ticket"], "main_flow")
        mx.expect(
            "C4",
            "七步流水线跑通（终态 run.finished）",
            "ChatBI",
            sse["terminal"] == "run.finished",
            f"terminal={sse['terminal']} events={len(sse['events'])}",
        )
        for step in (
            "intent",
            "schema_link",
            "sql_gen",
            "sql_guard",
            "sql_exec",
            "visual",
            "narrator",
        ):
            mx.expect(
                f"C4-{step}",
                f"步骤事件（agent.step.started 携带 step={step}）",
                "ChatBI",
                f'{{"step": "{step}"' in sse["raw"],
            )
        mx.expect(
            "C5",
            "SSE seq 单调递增",
            "ChatBI",
            bool(sse["seq_monotonic"]),
            f"max_seq={sse['max_seq']}",
        )
        mx.expect(
            "C5b",
            "SSE 关键事件齐全（sql.generated / dashboard.spec.ready / 七步 started+finished）",
            "ChatBI",
            {
                "sql.generated",
                "dashboard.spec.ready",
                "agent.step.started",
                "agent.step.finished",
            }.issubset(set(sse["events"]))
            and sse["events"].count("agent.step.started") == 7,
            f"started={sse['events'].count('agent.step.started')} "
            f"finished={sse['events'].count('agent.step.finished')}",
        )
        mx.check(
            "C5d",
            "SSE 回放窗口对超长 Run 的影响（run.started 是否仍可回放）",
            "ChatBI",
            "pass" if "run.started" in sse["events"] else "skip",
            f"frames={sse['frames']}（回放窗口 200 条，narration.delta 洪泛时会挤掉早期事件）",
        )
        mx.expect(
            "C5c",
            "SQL 命中演示事实表与口径字段",
            "ChatBI",
            "fact_sales" in sse["raw"] and "gmv_ex_tax" in sse["raw"],
        )

        dash_id = None
        for line in sse["raw"].splitlines():
            if line.startswith("data:") and "dashboard_id" in line:
                try:
                    dash_id = json.loads(line[5:]).get("dashboard_id")
                except json.JSONDecodeError:
                    pass
        if dash_id:
            status, spec = mx.http("GET", f"/api/v1/dashboards/{dash_id}", token=token)
            panels = (spec or {}).get("panels") or [] if isinstance(spec, dict) else []
            rows = sum(len(d.get("rows") or []) for d in ((spec or {}).get("data_sources") or []))
            mx.expect(
                "C6",
                "大屏 spec 落库（面板 ≥1 且数据行 ≥1）",
                "ChatBI",
                status == 200 and len(panels) >= 1 and rows >= 1,
                f"panels={len(panels)} rows={rows}",
            )
            mx.evidence("dashboard_spec", spec)
            status, versions = mx.http("GET", f"/api/v1/dashboards/{dash_id}/versions", token=token)
            mx.expect("C7", "大屏版本列表", "ChatBI", status == 200, f"{status}")
            status, _ = mx.http("POST", f"/api/v1/dashboards/{dash_id}/publish", {}, token)
            mx.expect("C7b", "大屏发布（publish）", "ChatBI", status in {200, 201}, f"{status}")
            status, exported = mx.http(
                "POST", f"/api/v1/dashboards/{dash_id}/export", {"format": "pdf"}, token
            )
            mx.expect("C7c", "大屏导出（export）", "ChatBI", status in {200, 201}, f"{status}")
            mx.evidence("dashboard_export", exported)
            status, bad = mx.http(
                "POST", f"/api/v1/dashboards/{dash_id}/export", {"format": "json"}, token
            )
            mx.expect(
                "C7d",
                "大屏导出非法 format → 400（入参校验）",
                "ChatBI",
                status == 400,
                f"{status} {json.dumps(bad, ensure_ascii=False)[:80] if bad else ''}",
            )
        else:
            mx.check("C6", "大屏 spec 落库", "ChatBI", "fail", "SSE 未携带 dashboard_id")

        status, audit = mx.http("GET", "/api/v1/chat/sql-audit", token=token)
        mx.expect("C8", "SQL 审计列表", "ChatBI", status == 200, f"{status}")
        mx.evidence("sql_audit", audit)

        status, _ = mx.http("GET", f"/api/v1/stream/runs/{trace}?ticket=bogus", timeout=20)
        mx.expect("C9", "非法 ticket 订阅 → 401", "ChatBI", status == 401, f"{status}")
        status, ticket_body = mx.http(
            "POST", "/api/v1/chat/stream-ticket", {"trace_id": trace}, token
        )
        other_ticket = (
            (ticket_body or {}).get("stream_ticket") if isinstance(ticket_body, dict) else None
        )
        if other_ticket:
            status, _ = mx.http(
                "GET",
                f"/api/v1/stream/runs/{trace}?ticket={other_ticket}&last_event_id=999999",
                timeout=30,
            )
            mx.expect(
                "C9b",
                "旧游标订阅不悬挂（缺口显式提示后结束）",
                "ChatBI",
                status == 200,
                f"{status}",
            )

        cancel_run = mx.start_run(token, "近 30 天 GMV 趋势（取消用）")
        if "error" not in cancel_run:
            mx.http(
                "POST",
                f"/api/v1/chat/sessions/{cancel_run['session_id']}/query/cancel",
                {"trace_id": cancel_run["trace_id"]},
                token,
            )
            sse2 = mx.sse(cancel_run["trace_id"], cancel_run["ticket"], "cancel_run", timeout=60)
            mx.check(
                "C10",
                "协作式取消 → run.aborted 或正常结束",
                "ChatBI",
                "pass" if sse2["terminal"] in {"run.aborted", "run.finished"} else "fail",
                f"terminal={sse2['terminal']}",
            )

    if sid:
        status, _ = mx.http("DELETE", f"/api/v1/chat/sessions/{sid}", token=token)
        mx.expect("C11", "会话软删除", "ChatBI", status == 200, f"{status}")


def section_chatbi_real(mx: Matrix) -> None:
    """真实模型路径（非确定性）：不逐字断言，只验证「可用 + 合规 + 随问题变化」。"""
    token = mx.tokens.get("admin", "")
    runs: list[dict[str, Any]] = []
    questions = ["近 30 天 GMV 趋势", "各品类销售占比怎么样"]
    for idx, question in enumerate(questions, start=1):
        run = mx.start_run(token, question, title=f"e2e-real-{idx}")
        if "error" in run:
            mx.check(
                f"C-R{idx}", f"真实模型 Run {idx} 启动", "ChatBI·真实模型", "fail", run["error"]
            )
            continue
        sse = mx.sse(run["trace_id"], run["ticket"], f"real_model_{idx}", timeout=300)
        if sse["terminal"] != "run.finished":
            code = ""
            for line in sse["raw"].splitlines():
                if line.startswith("data:") and '"code"' in line:
                    code = line[:160]
            mx.check(
                f"C-R{idx}",
                f"真实模型提问「{question}」跑到成功终态",
                "ChatBI·真实模型",
                "fail",
                f"terminal={sse['terminal']} {code}",
            )
            continue
        mx.check(
            f"C-R{idx}",
            f"真实模型提问「{question}」跑到成功终态",
            "ChatBI·真实模型",
            "pass",
            f"events={len(sse['events'])}",
        )
        upper = sse["raw"].upper()
        read_only = not any(
            k in upper for k in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ")
        )
        mx.check(
            f"C-R{idx}b",
            "真实模型生成的 SQL 为只读（未被护栏拒绝）",
            "ChatBI·真实模型",
            "pass" if read_only else "fail",
            "",
        )
        dash_id = None
        for line in sse["raw"].splitlines():
            if line.startswith("data:") and "dashboard_id" in line:
                try:
                    dash_id = json.loads(line[5:]).get("dashboard_id")
                except json.JSONDecodeError:
                    pass
        if dash_id:
            status, spec = mx.http("GET", f"/api/v1/dashboards/{dash_id}", token=token)
            panels = (spec or {}).get("panels") or []
            refs = {
                str((p.get("dataset") or {}).get("ref"))
                for p in panels
                if isinstance(p, dict) and p.get("dataset")
            }
            available = {str(ds.get("ref")) for ds in ((spec or {}).get("data_sources") or [])}
            mx.check(
                f"C-R{idx}c",
                "面板 ≥2 且每个 dataset.ref 都能在 data_sources 中找到",
                "ChatBI·真实模型",
                "pass" if len(panels) >= 2 and refs.issubset(available) else "fail",
                f"panels={len(panels)} refs={sorted(refs)} available={sorted(available)}",
            )
            mx.evidence(f"real_dashboard_{idx}", spec)
            runs.append({"question": question, "dashboard_id": dash_id, "refs": sorted(refs)})
        else:
            mx.check(f"C-R{idx}c", "大屏落库", "ChatBI·真实模型", "fail", "SSE 未携带 dashboard_id")
    if len(runs) == 2:
        mx.check(
            "C-R3",
            "大屏随问题变化（两次提问的 dashboard_id 不同）",
            "ChatBI·真实模型",
            "pass" if runs[0]["dashboard_id"] != runs[1]["dashboard_id"] else "fail",
            f"{runs[0]['dashboard_id']} vs {runs[1]['dashboard_id']}",
        )
    mx.evidence("real_model_runs", runs)


def section_obs(mx: Matrix) -> None:
    token = mx.tokens.get("admin", "")
    endpoints = [
        ("D1", "/api/v1/obs/overview", "观测总览"),
        ("D2", "/api/v1/obs/topology", "拓扑"),
        ("D3", "/api/v1/obs/agents", "Agent 清单"),
        ("D4", "/api/v1/obs/runs", "Runs 列表"),
        ("D5", "/api/v1/obs/self-cost", "平台自身成本"),
        ("D6", "/api/v1/obs/metrics/timeseries", "指标时序"),
        ("D7", "/api/v1/obs/metrics/skills", "技能指标"),
        ("D8", "/api/v1/obs/metrics/memory", "记忆指标"),
        ("D9", "/api/v1/obs/skills", "技能清单"),
        ("D10", "/api/v1/obs/anomalies", "异常事件列表"),
    ]
    loaded: dict[str, Any] = {}
    for cid, path, name in endpoints:
        status, body = mx.http("GET", path, token=token)
        loaded[path] = body
        mx.expect(cid, f"{name} → 200", "Observability", status == 200, f"{status}")
    mx.evidence("obs_overview", loaded.get("/api/v1/obs/overview"))
    mx.evidence("obs_runs", loaded.get("/api/v1/obs/runs"))
    mx.evidence("obs_anomalies", loaded.get("/api/v1/obs/anomalies"))
    mx.evidence("obs_memory_metrics", loaded.get("/api/v1/obs/metrics/memory"))
    memory_metrics = loaded.get("/api/v1/obs/metrics/memory")
    mx.check(
        "D8b",
        "记忆指标返回有效内容（后端日志报 'float' object is not callable 降级）",
        "Observability",
        "pass" if isinstance(memory_metrics, dict) and memory_metrics else "fail",
        f"type={type(memory_metrics).__name__} "
        f"body={json.dumps(memory_metrics, ensure_ascii=False)[:90]}",
    )

    kpi = (loaded.get("/api/v1/obs/overview") or {}).get("kpi_cards")
    mx.check(
        "D1b",
        "overview KPI 契约（问题 2 数据面复验）",
        "Observability",
        "pass" if isinstance(kpi, dict) else "fail",
        f"kpi_cards={type(kpi).__name__}",
    )
    if isinstance(kpi, dict):
        scalar = all(not isinstance(v, dict) or "value" in v for v in kpi.values())
        mx.check(
            "D1c",
            "KPI 字段均为标量或 {value,label} 形态（前端可取 .value）",
            "Observability",
            "pass" if scalar else "fail",
            f"keys={sorted(kpi)}",
        )

    rows = loaded.get("/api/v1/obs/runs")
    rows = rows if isinstance(rows, list) else []
    mx.expect("D4b", "Runs 列表含记录", "Observability", len(rows) >= 1, f"{len(rows)} 条")
    if rows:
        trace = str((rows[0] or {}).get("trace_id") or "")
        status, detail = mx.http("GET", f"/api/v1/obs/runs/{trace}", token=token)
        mx.check(
            "D4c",
            "Run 详情非空（问题 3 复验）",
            "Observability",
            "pass" if status == 200 and isinstance(detail, dict) and bool(detail) else "fail",
            f"status={status} keys={len(detail) if isinstance(detail, dict) else 'n/a'}",
        )
        mx.evidence("obs_run_detail", detail)
        failed = [r for r in rows if isinstance(r, dict) and r.get("status") == "failed"]
        if failed:
            with_code = [r for r in failed if r.get("error_code")]
            mx.check(
                "D4d",
                "失败 Run 的 error_code 已回填（问题 9 复验）",
                "Observability",
                "pass" if with_code else "fail",
                f"{len(with_code)}/{len(failed)} 条带 error_code",
            )
        else:
            mx.check(
                "D4d", "失败 Run 的 error_code 回填", "Observability", "skip", "当前无失败 Run"
            )

    alerts = loaded.get("/api/v1/obs/anomalies")
    alerts = alerts if isinstance(alerts, list) else []
    if alerts:
        alert_id = str((alerts[0] or {}).get("id"))
        status, _ = mx.http("GET", f"/api/v1/obs/anomalies/{alert_id}", token=token)
        mx.expect("D10b", "异常详情 → 200", "Observability", status == 200, f"{status}")
        status, acked = mx.http("POST", f"/api/v1/obs/anomalies/{alert_id}/ack", {}, token)
        mx.expect(
            "D10c",
            "异常确认（ack）",
            "Observability",
            status == 200 and (acked or {}).get("ok") is True,
            f"{status}",
        )
        status, resolved = mx.http("POST", f"/api/v1/obs/anomalies/{alert_id}/resolve", {}, token)
        mx.expect(
            "D10d",
            "异常解决（resolve）",
            "Observability",
            status == 200 and (resolved or {}).get("ok") is True,
            f"{status}",
        )
    else:
        mx.check("D10b", "异常详情/确认/解决", "Observability", "skip", "当前无异常记录（空态）")

    status, _ = mx.http("POST", "/api/v1/obs/otel/v1/traces", {"resourceSpans": []})
    mx.expect("D11", "OTel traces 无 token → 401", "Observability", status == 401, f"{status}")
    status, _ = mx.http("POST", "/api/v1/obs/otel/v1/traces", {"resourceSpans": []}, token=token)
    mx.expect(
        "D11b",
        "OTel traces 仅 Bearer token（无 X-Agent-Token）→ 401",
        "Observability",
        status == 401,
        f"{status}",
    )
    status, _ = mx.http(
        "POST",
        "/api/v1/obs/otel/v1/traces",
        {"resourceSpans": []},
        headers={"X-Agent-Token": "e2e-any-token"},
    )
    mx.expect(
        "D11c",
        "OTel traces 携带 X-Agent-Token 可接入",
        "Observability",
        status in {200, 202},
        f"{status}",
    )
    status, registered = mx.http(
        "POST",
        "/api/v1/obs/agents/register",
        {"agent_uid": "e2e-probe-agent", "name": "e2e-probe", "module": "chatbi"},
        headers={"X-Agent-Token": "e2e-arbitrary-token"},
    )
    mx.check(
        "D11d",
        "AgentToken 仅校验存在性、不校验取值（任意非空即可注册）",
        "Observability",
        "pass" if status in {200, 201} else "fail",
        f"status={status}（安全观察：建议改为共享密钥校验）",
    )
    mx.evidence("otel_ingest_result", registered)


def section_finops(mx: Matrix) -> None:
    token = mx.tokens.get("admin", "")
    analyst = mx.tokens.get("analyst", "")

    endpoints = [
        ("E1", "/api/v1/finops/cost/summary", "成本汇总"),
        ("E2", "/api/v1/finops/cost/timeseries", "成本时序"),
        ("E3", "/api/v1/finops/cost/top-spenders", "Top 花钱方"),
        ("E4", "/api/v1/finops/cost/coverage", "计价覆盖率"),
        ("E5", "/api/v1/finops/cache/stats", "缓存统计"),
        ("E6", "/api/v1/finops/curve/reuse-vs-token", "复用率曲线"),
        ("E7", "/api/v1/finops/budgets", "预算列表"),
        ("E8", "/api/v1/finops/prices", "价格表"),
        ("E9", "/api/v1/finops/recommendations", "优化建议"),
        ("E10", "/api/v1/finops/guardrail/policy", "护栏策略"),
        ("E11", "/api/v1/finops/loop-alerts", "死循环告警"),
    ]
    loaded: dict[str, Any] = {}
    for cid, path, name in endpoints:
        status, body = mx.http("GET", path, token=token)
        loaded[path] = body
        mx.expect(cid, f"{name} → 200", "FinOps", status == 200, f"{status}")
    mx.evidence("finops_summary", loaded.get("/api/v1/finops/cost/summary"))
    mx.evidence("finops_coverage", loaded.get("/api/v1/finops/cost/coverage"))
    mx.evidence("finops_budgets", loaded.get("/api/v1/finops/budgets"))
    mx.evidence("finops_policy", loaded.get("/api/v1/finops/guardrail/policy"))

    status, _ = mx.http("GET", "/api/v1/finops/cost/attribution", token=token)
    mx.expect("E12", "成本归因缺 trace_id → 400", "FinOps", status == 400, f"{status}")
    status, attribution = mx.http(
        "GET", "/api/v1/finops/cost/attribution?trace_id=e2e-none", token=token
    )
    mx.expect("E12b", "成本归因（未知 trace）不 5xx", "FinOps", status in {200, 404}, f"{status}")
    mx.evidence("finops_attribution", attribution)

    stamp = str(int(time.time()))
    status, created = mx.http(
        "POST",
        "/api/v1/finops/budgets",
        {
            "scope_type": "GLOBAL",
            "scope_id": f"e2e-{stamp}",
            "period": "DAY",
            "amount_micro_usd": 5000000,
            "soft_limit_pct": 80,
            "hard_limit_pct": 90,
            "soft_action": "ALERT",
            "hard_action": "BLOCK",
            "enabled": 1,
        },
        token,
    )
    mx.expect("E13", "预算创建（admin）", "FinOps", status in {200, 201}, f"{status} {created}")
    bid = (created or {}).get("budget_id") if isinstance(created, dict) else None
    if bid:
        status, patched = mx.http(
            "PATCH", f"/api/v1/finops/budgets/{bid}", {"amount_micro_usd": 9000000}, token
        )
        mx.expect("E13b", "预算修改（版本 +1）", "FinOps", status == 200, f"{status} {patched}")
        status, usage = mx.http("GET", f"/api/v1/finops/budgets/{bid}/usage", token=token)
        mx.expect("E13c", "预算用量查询", "FinOps", status == 200, f"{status}")
        mx.evidence("finops_budget_usage", usage)
        status, _ = mx.http("PATCH", "/api/v1/finops/budgets/99999999", {"enabled": 0}, token)
        mx.expect("E13d", "预算不存在 → 404", "FinOps", status == 404, f"{status}")
    status, bad_budget = mx.http(
        "POST", "/api/v1/finops/budgets", {"scope_type": "GLOBAL", "bogus_field": 1}, token
    )
    mx.check(
        "E13e",
        "预算写入非法/错名字段应返回 400（入参校验）",
        "FinOps",
        "pass" if status == 400 else "fail",
        f"实得 {status} {json.dumps(bad_budget, ensure_ascii=False)[:90] if bad_budget else ''}",
    )

    status, _ = mx.http("POST", "/api/v1/finops/budgets", {}, token=analyst)
    mx.expect("E14", "预算创建（analyst）→ 403 越权", "FinOps", status == 403, f"{status}")

    status, price = mx.http(
        "POST",
        "/api/v1/finops/prices",
        {
            "provider": f"e2e-{stamp}",
            "model": "e2e-model",
            "billing_unit": "PER_1K_TOKEN",
            "input_price_micro_usd": 1,
            "output_price_micro_usd": 2,
            "cache_read_price_micro_usd": 0,
            "cache_write_price_micro_usd": 0,
            "currency": "USD",
            "fx_rate_to_usd": 1.0,
            "effective_from": "2026-01-01T00:00:00",
        },
        token,
    )
    mx.expect("E15", "价格创建（admin）", "FinOps", status in {200, 201}, f"{status} {price}")
    status, bad_price = mx.http(
        "POST", "/api/v1/finops/prices", {"provider": "e2e", "bogus": 1}, token
    )
    mx.check(
        "E15c",
        "价格写入非法/错名字段应返回 400（入参校验）",
        "FinOps",
        "pass" if status == 400 else "fail",
        f"实得 {status} {json.dumps(bad_price, ensure_ascii=False)[:90] if bad_price else ''}",
    )
    status, sync = mx.http("POST", "/api/v1/finops/prices/sync", {}, token)
    mx.expect("E15b", "价格同步（sync）", "FinOps", status in {200, 201, 202}, f"{status} {sync}")
    status, recompute = mx.http("POST", "/api/v1/finops/cost/recompute", {}, token)
    mx.expect(
        "E16", "成本重算（recompute）", "FinOps", status in {200, 202}, f"{status} {recompute}"
    )

    policy = loaded.get("/api/v1/finops/guardrail/policy")
    mx.expect("E17", "护栏策略读取", "FinOps", isinstance(policy, dict), f"{type(policy).__name__}")
    if isinstance(policy, dict):
        status, _ = mx.http(
            "PATCH",
            "/api/v1/finops/guardrail/policy",
            {"allow_circuit_break": policy.get("allow_circuit_break", False)},
            token,
        )
        mx.expect("E17b", "护栏策略修改（PATCH）", "FinOps", status == 200, f"{status}")
    status, kill_on = mx.http(
        "POST", "/api/v1/finops/guardrail/kill-switch", {"enabled": True}, token
    )
    mx.expect("E18", "硬熔断开关：开启", "FinOps", status == 200, f"{status} {kill_on}")
    status, _ = mx.http("POST", "/api/v1/finops/guardrail/kill-switch", {"enabled": False}, token)
    mx.expect("E18b", "硬熔断开关：关闭（恢复默认）", "FinOps", status == 200, f"{status}")
    status, _ = mx.http("POST", "/api/v1/finops/guardrail/kill-switch", {"enabled": False}, analyst)
    mx.expect("E18c", "护栏写操作（analyst）→ 403 越权", "FinOps", status == 403, f"{status}")

    items = loaded.get("/api/v1/finops/recommendations")
    items = items if isinstance(items, list) else []
    if items:
        reco_id = str((items[0] or {}).get("reco_id"))
        status, applied = mx.http(
            "POST", f"/api/v1/finops/recommendations/{reco_id}/apply", {"dry_run": True}, token
        )
        mx.expect("E19", "建议 apply（dry_run）", "FinOps", status in {200, 202}, f"{status}")
        mx.evidence("finops_reco_apply", applied)
        status, rejected = mx.http(
            "POST", f"/api/v1/finops/recommendations/{reco_id}/reject", {}, token
        )
        mx.expect("E19c", "建议 reject", "FinOps", status in {200, 202}, f"{status}")
    else:
        mx.check("E19", "建议 apply / reject", "FinOps", "skip", "当前无建议（空态）")
    status, _ = mx.http("POST", "/api/v1/finops/recommendations/none/apply", {}, token)
    mx.expect("E19b", "建议不存在时 apply 不 5xx", "FinOps", status in {400, 404, 200}, f"{status}")

    imported: list[str] = []
    if bid:
        imported.append(f"budget(id={bid})")
    dsn = os.environ.get("DBA_E2E_DSN", "")
    if dsn:
        try:
            # ★ 先删子表（budget_usage 有外键引用 budget.id），再删父表
            asyncio.run(
                _mysql_exec(
                    dsn,
                    "DELETE FROM budget_usage WHERE budget_id IN "
                    f"(SELECT id FROM budget WHERE scope_id = 'e2e-{stamp}')",
                )
            )
            asyncio.run(_mysql_exec(dsn, f"DELETE FROM budget WHERE scope_id = 'e2e-{stamp}'"))
            asyncio.run(_mysql_exec(dsn, f"DELETE FROM price_book WHERE provider = 'e2e-{stamp}'"))
            mx.check(
                "E20",
                "FinOps 测试数据清理（按 e2e- 前缀删除）",
                "FinOps",
                "pass",
                f"已清理 budget/price_book（前缀 e2e-{stamp}）",
            )
        except Exception as exc:  # noqa: BLE001
            mx.check("E20", "FinOps 测试数据清理", "FinOps", "fail", f"{type(exc).__name__}: {exc}")
    else:
        mx.check(
            "E20",
            "FinOps 测试数据清理",
            "FinOps",
            "skip",
            f"未提供 DBA_E2E_DSN，需手工清理前缀 e2e-{stamp}",
        )


def section_cross(mx: Matrix) -> None:
    token = mx.tokens.get("admin", "")
    run = mx.start_run(token, "近 30 天 GMV 趋势（跨模块）", title="e2e-cross")
    if "error" in run:
        mx.check("F1", "跨模块追踪 Run 启动", "跨模块", "fail", run["error"])
        return
    trace = run["trace_id"]
    sse = mx.sse(trace, run["ticket"], "cross_module")

    status, runs = mx.http("GET", "/api/v1/obs/runs", token=token)
    rows = runs if isinstance(runs, list) else []
    matched = [r for r in rows if isinstance(r, dict) and r.get("trace_id") == trace]
    mx.check(
        "F1",
        "同一 trace 在观测 Runs 可见（ChatBI→埋点→Observability）",
        "跨模块",
        "pass" if matched else "fail",
        f"trace={trace} matched={len(matched)}",
    )
    if matched:
        mx.evidence("cross_run_row", matched[0])

    status, detail = mx.http("GET", f"/api/v1/obs/runs/{trace}", token=token)
    mx.check(
        "F2",
        "Run 详情（span 树）可读",
        "跨模块",
        "pass" if status == 200 and isinstance(detail, dict) and detail else "fail",
        f"status={status}",
    )

    status, attribution = mx.http(
        "GET", f"/api/v1/finops/cost/attribution?trace_id={trace}", token=token
    )
    mx.check(
        "F3",
        "Cost 归因可查该 trace（ChatBI→FinOps）",
        "跨模块",
        "pass" if status == 200 else "fail",
        f"status={status}",
    )
    mx.evidence("cross_cost_attribution", attribution)
    mx.check(
        "F4",
        "Run 终态与埋点一致",
        "跨模块",
        "pass" if sse["terminal"] in {"run.finished", "run.error"} else "fail",
        f"terminal={sse['terminal']}",
    )


GUARD_CASES = [
    ("G1", "bad_table", "SQL_TABLE_NOT_ALLOWED", "生成未登记物理表 → 护栏拒绝，重试耗尽"),
    ("G2", "not_readonly", "SQL_NOT_READONLY", "非只读语句 → 拒绝"),
    ("G3", "multi_stmt", "SQL_MULTI_STATEMENT", "多语句 → 拒绝"),
    ("G4", "dangerous_func", "SQL_DANGEROUS_FUNCTION", "高危函数 → 拒绝"),
    ("G5", "func_not_allowed", "SQL_FUNCTION_NOT_ALLOWED", "函数白名单外 → 拒绝"),
    ("G6", "dry_run_fail", "SQL_DRY_RUN_FAILED", "EXPLAIN 预执行失败 → 拒绝"),
]


def section_guard(mx: Matrix, guard_base: dict[str, str]) -> None:
    token = mx.tokens.get("admin", "")
    dynamic = guard_base.get("script")

    def base_for(mode: str) -> str:
        return dynamic or guard_base.get(mode, "")

    def select(mode: str) -> bool:
        """动态实例：切换脚本模式；静态实例：直接可用。"""
        if not dynamic:
            return True
        status, body = mx.http("POST", "/__e2e__/script", {"mode": mode}, base=dynamic)
        if status != 200:
            mx.check(
                f"G0-{mode}", f"切换脚本模式 {mode}", "ChatBI·护栏", "fail", f"{status} {body}"
            )
            return False
        return True

    for cid, mode, expect_code, name in GUARD_CASES:
        base = base_for(mode)
        if not base:
            mx.check(cid, name, "ChatBI·护栏", "skip", f"未提供 {mode} 实例")
            continue
        if not select(mode):
            continue
        run = mx.start_run(token, "近 30 天 GMV 趋势", title=f"e2e-{mode}", base=base)
        if "error" in run:
            mx.check(cid, name, "ChatBI·护栏", "fail", run["error"])
            continue
        sse = mx.sse(run["trace_id"], run["ticket"], f"guard_{mode}", timeout=180, base=base)
        mx.expect(
            cid,
            name,
            "ChatBI·护栏",
            expect_code in sse["raw"],
            f"terminal={sse['terminal']} 期望码={expect_code}",
        )
        mx.expect(
            f"{cid}b",
            f"{name} → 终态 failed（run.error + run.finished）",
            "ChatBI·护栏",
            "failed" in sse["raw"] and sse["terminal"] in {"run.finished", "run.error"},
            f"terminal={sse['terminal']}",
        )

    base = base_for("bad_table_then_ok")
    if not base:
        mx.check("G7", "自愈重生成分支", "ChatBI·护栏", "skip", "未提供 bad_table_then_ok 实例")
        return
    if not select("bad_table_then_ok"):
        return
    run = mx.start_run(token, "近 30 天 GMV 趋势", title="e2e-retry", base=base)
    if "error" in run:
        mx.check("G7", "自愈重生成分支", "ChatBI·护栏", "fail", run["error"])
        return
    sse = mx.sse(run["trace_id"], run["ticket"], "guard_retry_ok", timeout=180, base=base)
    mx.expect(
        "G7",
        "自愈重生成：首轮被拒 → 重试通过 → 成功出屏",
        "ChatBI·护栏",
        sse["terminal"] == "run.finished"
        and "sql.retry.resolved" in sse["events"]
        and "run.error" not in sse["events"],
        f"terminal={sse['terminal']} resolved={'sql.retry.resolved' in sse['events']}",
    )


async def _mysql_exec(dsn: str, sql: str, fetch: bool = False) -> list[tuple[Any, ...]]:
    import asyncmy  # type: ignore[import-not-found]

    parts = urllib.parse.urlparse(dsn.replace("mysql+asyncmy://", "mysql://"))
    conn = await asyncmy.connect(
        host=parts.hostname,
        port=parts.port or 3306,
        user=urllib.parse.unquote(parts.username or ""),
        password=urllib.parse.unquote(parts.password or ""),
        db=(parts.path or "/dba").lstrip("/"),
    )
    try:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            rows = list(await cur.fetchall()) if fetch else []
        await conn.commit()
        return rows
    finally:
        conn.close()


def section_scope(mx: Matrix, dsn: str, guard_base: dict[str, str]) -> None:
    """行级权限：注入 role→表→省份规则 → 验证真实注入 → 清理。"""
    if not dsn:
        mx.check("H0", "行级权限真实库注入", "行级权限", "skip", "未提供 --dsn")
        return
    token = mx.tokens.get("analyst", "")
    rule_id: int | None = None
    try:
        asyncio.run(
            _mysql_exec(
                dsn,
                "INSERT INTO row_scope_rule (role_id, physical_table, scope_column, operator, "
                "value_type, value_json, priority, enabled) VALUES "
                "(2, 'fact_sales', 'province_name', 'IN', 'STATIC', '[\"江苏省\"]', 1, 1)",
            )
        )
        rows = asyncio.run(
            _mysql_exec(
                dsn,
                "SELECT id FROM row_scope_rule WHERE role_id=2 AND physical_table='fact_sales' "
                "ORDER BY id DESC LIMIT 1",
                fetch=True,
            )
        )
        rule_id = int(rows[0][0]) if rows else None
        mx.check(
            "H1",
            "注入 role 2 → fact_sales.province_name 权限规则",
            "行级权限",
            "pass" if rule_id else "fail",
            f"rule_id={rule_id}",
        )
    except Exception as exc:  # noqa: BLE001
        mx.check("H1", "行级权限规则注入", "行级权限", "fail", f"{type(exc).__name__}: {exc}")
        return

    try:
        # H2：fail-closed —— SQL 不含权限列且无 join 路径 → 必须拒绝（不能放行）
        run = mx.start_run(
            token, "近 30 天 GMV 趋势", options={"_role_ids": [2]}, title="e2e-scope"
        )
        if "error" in run:
            mx.check("H2", "带角色上下文的 Run", "行级权限", "fail", run["error"])
        else:
            sse = mx.sse(run["trace_id"], run["ticket"], "row_scope_failclosed", timeout=180)
            raw = sse["raw"]
            mx.check(
                "H2",
                "fail-closed：无法安全注入时拒绝执行（SQL_SCOPE_JOIN_MISSING）",
                "行级权限",
                "pass" if "SQL_SCOPE_JOIN_MISSING" in raw else "fail",
                f"terminal={sse['terminal']}",
            )

        # H3：真实注入 —— SQL 含权限列 → 必须改写为 province_name IN ('江苏') 且只回该省数据
        dynamic = guard_base.get("script")
        base = dynamic or guard_base.get("province_ok", "")
        if not base:
            mx.check("H3", "行级谓词真实注入", "行级权限", "skip", "未提供 province_ok 实例")
        else:
            if dynamic:
                mx.http("POST", "/__e2e__/script", {"mode": "province_ok"}, base=dynamic)
            run2 = mx.start_run(
                token,
                "各省份 GMV 排名",
                options={"_role_ids": [2]},
                title="e2e-scope-inject",
                base=base,
            )
            if "error" in run2:
                mx.check("H3", "行级谓词真实注入", "行级权限", "fail", run2["error"])
            else:
                sse2 = mx.sse(
                    run2["trace_id"], run2["ticket"], "row_scope_inject", timeout=180, base=base
                )
                raw2 = sse2["raw"]
                injected = "province_name" in raw2 and "江苏" in raw2
                mx.check(
                    "H3",
                    "AST 注入行级谓词（SQL 出现 province_name IN ('江苏省')）",
                    "行级权限",
                    "pass" if injected else "fail",
                    "命中注入" if injected else "未见注入谓词",
                )
                mx.expect(
                    "H3b",
                    "注入后仍跑到成功终态",
                    "行级权限",
                    sse2["terminal"] == "run.finished",
                    f"terminal={sse2['terminal']}",
                )
                # 落库数据行必须只含权限内省份
                dash_id = None
                for line in raw2.splitlines():
                    if line.startswith("data:") and "dashboard_id" in line:
                        try:
                            dash_id = json.loads(line[5:]).get("dashboard_id")
                        except json.JSONDecodeError:
                            pass
                if dash_id:
                    status, spec = mx.http("GET", f"/api/v1/dashboards/{dash_id}", token=token)
                    provinces: set[str] = set()
                    total_rows = 0
                    for ds in (spec or {}).get("data_sources") or []:
                        cols = [str(c.get("name")) for c in (ds.get("columns") or [])]
                        idx = cols.index("province") if "province" in cols else None
                        for row in ds.get("rows") or []:
                            total_rows += 1
                            if isinstance(row, dict) and row.get("province") is not None:
                                provinces.add(str(row["province"]))
                            elif isinstance(row, list) and idx is not None and idx < len(row):
                                provinces.add(str(row[idx]))
                    mx.check(
                        "H3c",
                        "返回行集只含权限内省份（数据面证据）",
                        "行级权限",
                        "pass" if total_rows > 0 and provinces == {"江苏省"} else "fail",
                        f"rows={total_rows} provinces={sorted(provinces)}",
                    )
                    mx.evidence("row_scope_dashboard", spec)
    finally:
        if rule_id is not None:
            try:
                asyncio.run(_mysql_exec(dsn, f"DELETE FROM row_scope_rule WHERE id={int(rule_id)}"))
                mx.check("H4", "行级权限测试规则清理", "行级权限", "pass", f"已删除 {rule_id}")
            except Exception as exc:  # noqa: BLE001
                mx.check("H4", "行级权限测试规则清理", "行级权限", "fail", str(exc))


def section_fault(mx: Matrix, fault: str, fault_base: str) -> None:
    if not fault_base:
        mx.check("I0", "故障注入实例", "故障降级", "skip", "未提供 --fault-base")
        return
    token = mx.tokens.get("admin", "")
    status, body = mx.http("GET", "/ready", base=fault_base)
    state = (body or {}).get("status") if isinstance(body, dict) else None
    comps = (body or {}).get("components", {}) if isinstance(body, dict) else {}
    mx.evidence(f"fault_{fault}_ready", body)

    if fault == "redis":
        mx.expect(
            "I1",
            "/ready 反映 Redis 不可用（ok=false 或 status=degraded）",
            "故障降级",
            bool(comps) and (comps.get("redis", {}).get("ok") is False or state == "degraded"),
            f"status={state} redis={comps.get('redis')}",
        )
        status, _ = mx.http("GET", "/api/v1/finops/budgets", token=token, base=fault_base)
        mx.expect(
            "I2", "Redis 不可用时预算接口 fail-open（200）", "故障降级", status == 200, f"{status}"
        )
        status, _ = mx.http("GET", "/api/v1/obs/overview", token=token, base=fault_base)
        mx.expect(
            "I3", "Redis 不可用时观测总览不 500", "故障降级", status in {200, 503}, f"{status}"
        )
    elif fault == "mysql":
        mx.expect(
            "I1",
            "/ready 在 MySQL 不可用时揭示 mysql.ok=false",
            "故障降级",
            comps.get("mysql", {}).get("ok") is False,
            f"http={status} state={state} mysql={comps.get('mysql')}",
        )
        mx.check(
            "I1b",
            "就绪语义：仅当三个硬依赖全挂才 503（单 MySQL 挂 = degraded 200，按设计）",
            "故障降级",
            "pass" if status == 200 and state == "degraded" else "fail",
            f"http={status} state={state}（运维影响：写路径已断但仍被判就绪）",
        )
        status, body = mx.http("GET", "/api/v1/obs/runs", token=token, base=fault_base)
        mx.expect(
            "I2",
            "MySQL 不可用时列表接口降级空壳（200 + []）",
            "故障降级",
            status == 200 and isinstance(body, list),
            f"{status} type={type(body).__name__}",
        )
        status, body = mx.http(
            "POST",
            "/api/v1/chat/sessions/any/query",
            {"question": "近 30 天 GMV 趋势"},
            token,
            base=fault_base,
        )
        if status in {503, 500}:
            mx.check(
                "I3",
                "MySQL 不可用时提问直接拒绝（503/500）",
                "故障降级",
                "pass",
                f"同步状态={status}",
            )
        elif status == 200 and isinstance(body, dict) and body.get("trace_id"):
            sse = mx.sse(
                str(body["trace_id"]),
                str(body.get("stream_ticket") or ""),
                "fault_mysql_run",
                timeout=60,
                base=fault_base,
            )
            mx.check(
                "I3",
                "MySQL 不可用时提问被受理但终态必须失败（无静默假成功）",
                "故障降级",
                "pass" if sse["terminal"] == "run.error" else "fail",
                f"同步=200 异步 terminal={sse['terminal']}",
            )
        else:
            mx.check("I3", "MySQL 不可用时提问", "故障降级", "fail", f"同步状态={status}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="api_matrix")
    parser.add_argument("--out", required=True)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--llm", default="fake", choices=["fake", "real"])
    parser.add_argument("--sections", default="env,auth,chatbi,obs,finops,cross")
    parser.add_argument("--guard-base", action="append", default=[])
    parser.add_argument("--scope-rule", action="store_true")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--fault", default="none")
    parser.add_argument("--fault-base", default="")
    args = parser.parse_args(argv)

    mx = Matrix(base=args.base, out_dir=args.out, llm=args.llm)
    os.makedirs(f"{args.out}/evidence", exist_ok=True)
    sections = {s.strip() for s in args.sections.split(",") if s.strip()}
    guard_base = dict(item.split("=", 1) for item in args.guard_base if "=" in item)

    print(f"═══ 接口矩阵（llm={args.llm}, base={args.base}）═══")
    # ★ env / auth 是其余各段的引导（登录取 token），始终执行
    print("\n── A. 环境与基础可用性")
    section_env(mx)
    print("\n── B. 鉴权与越权")
    section_auth(mx)
    if "chatbi" in sections:
        if mx.llm == "real":
            print("\n── C. ChatBI 主链路（真实模型：放宽断言）")
            section_chatbi_real(mx)
        else:
            print("\n── C. ChatBI 主链路")
            section_chatbi(mx)
    if "obs" in sections:
        print("\n── D. Observability")
        section_obs(mx)
    if "finops" in sections:
        print("\n── E. FinOps")
        section_finops(mx)
    if "cross" in sections:
        print("\n── F. 跨模块数据交互")
        section_cross(mx)
    if "guard" in sections:
        print("\n── G. SQL 护栏分支（脚本化 LLM）")
        section_guard(mx, guard_base)
    if "scope" in sections:
        print("\n── H. 行级权限（真实库注入）")
        section_scope(mx, args.dsn or "", guard_base)
    if "fault" in sections:
        print("\n── I. 故障注入")
        section_fault(mx, args.fault, args.fault_base)

    with open(f"{args.out}/api_results.json", "w", encoding="utf-8") as handle:
        json.dump(mx.results, handle, ensure_ascii=False, indent=2)

    counts = {"pass": 0, "fail": 0, "skip": 0}
    for row in mx.results:
        counts[row["status"]] += 1
    summary = {
        "total": len(mx.results),
        "pass": counts["pass"],
        "fail": counts["fail"],
        "skip": counts["skip"],
        "failures": [
            {"id": r["id"], "name": r["name"], "detail": r["detail"]}
            for r in mx.results
            if r["status"] == "fail"
        ],
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if counts["fail"] else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
