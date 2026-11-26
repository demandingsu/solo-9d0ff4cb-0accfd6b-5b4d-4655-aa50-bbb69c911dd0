#!/usr/bin/env python3
"""散帖争用 / 交错确认 / 幂等重发 的 HTTP 并发自测。

只用 Python 标准库，直接打真实运行的 uvicorn + SQLite 服务：

    BASE_URL=http://127.0.0.1:8000 python3 selftest.py

服务端需要以 ENABLE_RESET=1 启动（compose 已默认打开），
脚本会在每个用例组之间调用 /api/admin/reset 清空数据。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from threading import Barrier
from typing import Any, Callable

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")

_passed = 0
_failed = 0
_failures: list[str] = []

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
_GREEN, _RED, _RESET = ("\033[32m", "\033[31m", "\033[0m") if _COLOR else ("", "", "")


def check(name: str, condition: bool, detail: Any = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  {_GREEN}PASS{_RESET}  {name}")
    else:
        _failed += 1
        _failures.append(name)
        print(f"  {_RED}FAIL{_RESET}  {name}  ::  {detail}")


def request(method: str, path: str, body: Any = None, timeout: float = 30.0):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return exc.code, payload
    except Exception as exc:  # 连接错误等
        return 0, {"error": repr(exc)}


def wait_health(timeout_s: float = 60.0) -> None:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        status, body = request("GET", "/health", timeout=5)
        if status == 200:
            print(f"健康检查通过: {body}")
            return
        last = (status, body)
        time.sleep(1)
    print(f"服务未就绪: {last}")
    sys.exit(2)


def reset() -> None:
    status, body = request("POST", "/api/admin/reset")
    if status == 404:
        print("重置接口不可用；请以 ENABLE_RESET=1 启动服务。")
        sys.exit(2)
    assert status == 200, (status, body)


# --------------------------------------------------------------------------- #
# 构造辅助
# --------------------------------------------------------------------------- #
def make_book(versions: list[Any], title: str = "自测书") -> int:
    status, body = request(
        "POST", "/api/books", {"title": title, "page_versions": versions}
    )
    assert status == 201, (status, body)
    return body["id"]


def add_samples(book_id: int, specs: list[tuple[str, int]]) -> dict[str, int]:
    status, body = request(
        "POST",
        f"/api/books/{book_id}/samples",
        {"samples": [{"code": c, "bound_to_page": b} for c, b in specs]},
    )
    assert status == 201, (status, body)
    return {s["code"]: s["id"] for s in body["samples"]}


def add_pile(
    book_id: int, code: str, start: int, versions: list[Any], copies: int
) -> int:
    status, body = request(
        "POST",
        f"/api/books/{book_id}/piles",
        {
            "code": code,
            "start_page": start,
            "page_versions": versions,
            "copies": copies,
        },
    )
    assert status == 201, (status, body)
    return body["id"]


def trial(book_id: int, sample_ids: list[int] | None = None) -> dict[str, Any]:
    status, body = request(
        "POST",
        f"/api/books/{book_id}/trial-matches",
        {"sample_ids": sample_ids or []},
    )
    assert status == 200, (status, body)
    return body


def confirm_body(
    book_id: int,
    picks: list[tuple[int, list[int]]],
    request_id: str | None = None,
) -> dict[str, Any]:
    return {
        "book_id": book_id,
        "request_id": request_id,
        "pick_lists": [
            {"sample_id": sid, "items": [{"pile_id": pid} for pid in pids]}
            for sid, pids in picks
        ],
    }


def confirm(body: dict[str, Any]):
    return request("POST", "/api/requisitions", body)


def confirm_plan(plan_id: str):
    return request("POST", f"/api/plans/{plan_id}/confirm")


def piles_map(book_id: int) -> dict[int, dict[str, Any]]:
    status, body = request("GET", f"/api/books/{book_id}/piles")
    assert status == 200, (status, body)
    return {p["id"]: p for p in body}


def sample_status_map(book_id: int) -> dict[int, str]:
    status, body = request("GET", f"/api/books/{book_id}/samples")
    assert status == 200, (status, body)
    return {s["id"]: s["status"] for s in body}


def order_count(book_id: int) -> int:
    status, body = request("GET", f"/api/requisitions?book_id={book_id}")
    assert status == 200, (status, body)
    return len(body)


def run_concurrent(n: int, fn: Callable[[Barrier, int], Any]) -> list[Any]:
    barrier = Barrier(n)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(fn, barrier, i) for i in range(n)]
        return [f.result(timeout=60) for f in futures]


def validate_pick_lists(result: dict[str, Any], target: list[str]) -> None:
    """校验试配出的每一条取帖单：缺页起步、连续、到末页、版号相符。"""
    n_pages = len(target)
    for pl in result["pick_lists"]:
        cursor = pl["bound_to_page"] + 1
        check(
            f"取帖单 {pl['sample_code']} 从缺页 {cursor} 起步",
            pl["missing_from_page"] == cursor
            and (not pl["items"] or pl["items"][0]["start_page"] == cursor),
            pl,
        )
        for item in pl["items"]:
            check(
                f"{pl['sample_code']} 帖 {item['pile_code']} 与前段连续",
                item["start_page"] == cursor,
                (cursor, item),
            )
            editions_ok = all(
                str(target[item["start_page"] - 1 + j]) == str(ed)
                for j, ed in enumerate(item["page_editions"])
            )
            check(
                f"{pl['sample_code']} 帖 {item['pile_code']} 逐页版号相符",
                editions_ok,
                item["page_editions"],
            )
            cursor = item["end_page"] + 1
        check(
            f"{pl['sample_code']} 连续接到末页 {n_pages}",
            cursor == n_pages + 1,
            {"cursor": cursor},
        )

    # 分配不超过快照余量。
    usage: dict[int, int] = {}
    for pl in result["pick_lists"]:
        for item in pl["items"]:
            usage[item["pile_id"]] = usage.get(item["pile_id"], 0) + 1
    snap = {p["pile_id"]: p["copies_remaining"] for p in result["stock_snapshot"]}
    check(
        "试配聚合用量不超过任何一垛的快照余量",
        all(q <= snap[pid] for pid, q in usage.items()),
        {"usage": usage, "snapshot": snap},
    )


# --------------------------------------------------------------------------- #
# A. 最大流试配 + 确认 + 幂等重发 + 已领料冲突
# --------------------------------------------------------------------------- #
def group_a() -> None:
    print("\n=== A. 最大同时补齐数 / 确认 / 幂等重发 / 领料状态冲突 ===")
    reset()
    target = ["E1", "E1", "E2", "E2", "E3", "E3"]
    book = make_book(target, "A-争用演示")
    smap = add_samples(book, [(f"A{i}", b) for i, b in enumerate([0, 2, 2, 2, 4], 1)])
    p12 = add_pile(book, "T12", 1, ["E1", "E1"], 2)
    p34 = add_pile(book, "T34", 3, ["E2", "E2"], 1)
    p36 = add_pile(book, "T36", 3, ["E2", "E2", "E3", "E3"], 2)
    p56 = add_pile(book, "T56", 5, ["E3", "E3"], 2)
    bad = add_pile(book, "BAD", 3, ["E0", "E0"], 3)

    before = piles_map(book)
    result = trial(book)
    check("候选样书 5 本", result["candidate_count"] == 5, result["candidate_count"])
    check("最大同时补齐数 = 4", result["selected_count"] == 4, result["selected_count"])
    check(
        "恰好 1 本因争用未入选",
        len(result["unselected_samples"]) == 1
        and result["unselected_samples"][0]["reason"] == "contention",
        result["unselected_samples"],
    )
    bad_diag = [d for d in result["pile_diagnostics"] if d["pile_id"] == bad]
    check(
        "版号不符散帖被诊断为 edition_mismatch",
        bool(bad_diag) and bad_diag[0]["reason"] == "edition_mismatch",
        bad_diag,
    )
    after_trial = piles_map(book)
    check(
        "试配不扣余量（不占纸）",
        all(
            before[k]["copies_remaining"] == after_trial[k]["copies_remaining"]
            for k in before
        ),
    )
    validate_pick_lists(result, target)

    unselected_id = result["unselected_samples"][0]["sample_id"]
    selected_codes = {pl["sample_code"] for pl in result["pick_lists"]}
    # Dinic 分层图会优先路由路径更短的 A2/A3/A4/A5（page3 直接从样书边得到
    # level 2，A1 经 T12 到 page3 的边在该层图不可增广），这是合法的确定性最大流。
    check(
        "入选集合为确定性的 A2,A3,A4,A5",
        selected_codes == {"A2", "A3", "A4", "A5"},
        selected_codes,
    )
    result_again = trial(book)
    check(
        "同一时点重复试配结果稳定（同一最大流方案）",
        result_again["selected_sample_ids"] == result["selected_sample_ids"]
        and [p["pile_id"] for pl in result_again["pick_lists"] for p in pl["items"]]
        == [p["pile_id"] for pl in result["pick_lists"] for p in pl["items"]],
    )

    # 版号不符的帖不能进确认单 -> 422（此时所有样书仍可领料）
    a2 = smap["A2"]
    status, bad_resp = confirm(confirm_body(book, [(a2, [bad])]))
    check("版号不符的取帖单被语义校验拒绝(422)", status == 422, (status, bad_resp))
    check(
        "错误码 invalid_pick_list / edition_mismatch",
        bad_resp["detail"]["code"] == "invalid_pick_list"
        and bad_resp["detail"]["problems"][0]["problem"] == "edition_mismatch",
        bad_resp.get("detail"),
    )

    # 确认试配方案
    status, body = confirm_plan(result["plan_id"])
    check("确认方案返回 201", status == 201, (status, body))
    order = body["order"]
    order_id = order["id"]
    check("领料单号格式 LL-0001", order["order_no"] == "LL-0001", order["order_no"])
    check("replayed=false", body["replayed"] is False, body)

    after_confirm = piles_map(book)
    expected_remaining = {p12: 2, p34: 0, p36: 0, p56: 0, bad: 3}
    check(
        "扣料后余量正确（T12=2 未用,T34=0,T36=0,T56=0,BAD=3）",
        all(after_confirm[pid]["copies_remaining"] == rem for pid, rem in expected_remaining.items()),
        {pid: after_confirm[pid]["copies_remaining"] for pid in expected_remaining},
    )
    statuses = sample_status_map(book)
    check(
        "4 本入选样书均标记已领料，落选本仍可领",
        sum(v == "requisitioned" for v in statuses.values()) == 4
        and statuses[unselected_id] == "available",
        statuses,
    )

    # 重发 1：再次 plan confirm
    status, body2 = confirm_plan(result["plan_id"])
    check("重发方案确认返回 200", status == 200, (status, body2))
    check(
        "重发返回同一张原领料单",
        body2["replayed"] is True and body2["order"]["id"] == order_id,
        body2,
    )
    check(
        "重发原因 plan_already_confirmed",
        body2["replay_reason"] == "plan_already_confirmed",
        body2.get("replay_reason"),
    )

    # 重发 2：原样 POST /api/requisitions（无 request_id，靠指纹）
    status, body3 = confirm(result["confirm_request"])
    check("原样重发领料请求返回 200", status == 200, (status, body3))
    check(
        "指纹幂等返回原单且原因 same_plan",
        body3["replayed"] is True
        and body3["replay_reason"] == "same_plan"
        and body3["order"]["id"] == order_id,
        body3,
    )
    after_replay = piles_map(book)
    check(
        "两次重发都没有二次扣料",
        all(
            after_replay[k]["copies_remaining"] == after_confirm[k]["copies_remaining"]
            for k in after_confirm
        ),
    )

    # 已领料样书冲突：A2 已领，和落选本拼单 -> 409，状态/余量照旧
    conflict_body = confirm_body(
        book, [(unselected_id, [p36]), (a2, [p36])]
    )
    stock_before_conflict = {
        k: p["copies_remaining"] for k, p in piles_map(book).items()
    }
    status, body4 = confirm(conflict_body)
    check("含已领料样书的确认返回 409", status == 409, (status, body4))
    check(
        "错误码 samples_already_requisitioned",
        body4["detail"]["code"] == "samples_already_requisitioned"
        and any(s["sample_id"] == a2 for s in body4["detail"]["samples"]),
        body4.get("detail"),
    )
    check(
        "冲突后余量照旧",
        {k: p["copies_remaining"] for k, p in piles_map(book).items()}
        == stock_before_conflict,
    )
    check(
        "冲突后落选样书仍是 available",
        sample_status_map(book)[unselected_id] == "available",
    )

    # 断链/错帖不能进确认单 -> 422（落选本是 bound=0 的 A1，BAD 从第 3 页起，先撞连续性）
    status, body5 = confirm(confirm_body(book, [(unselected_id, [bad])]))
    check("断链取帖单被语义校验拒绝(422)", status == 422, (status, body5))
    check(
        "错误码 invalid_pick_list / chain_not_continuous",
        body5["detail"]["code"] == "invalid_pick_list"
        and body5["detail"]["problems"][0]["problem"] == "chain_not_continuous",
        body5.get("detail"),
    )
    check("A 组最终只有 1 张领料单", order_count(book) == 1, order_count(book))

    # 确认后再试配：4 本已领料被跳过，唯一未领本因 tails 无余量而 0 入选；
    # 连续两次 0 入选试配不能因指纹重复而 500，空方案也不能确认。
    z1 = trial(book)
    z2 = trial(book)
    check(
        "确认后再试配为 0 入选，落选原因 out_of_stock",
        z1["selected_count"] == 0
        and z1["unselected_samples"][0]["reason"] == "out_of_stock",
        z1.get("unselected_samples"),
    )
    check("重复 0 入选试配仍返回独立 plan_id", z1["plan_id"] != z2["plan_id"])
    zs, zb = confirm_plan(z1["plan_id"])
    check("空方案确认被拒绝(422 empty_plan)", zs == 422 and zb["detail"]["code"] == "empty_plan", (zs, zb))
    check("空方案拒绝后领料单仍只有 1 张", order_count(book) == 1)


# --------------------------------------------------------------------------- #
# B. 散帖争用：5 张并发单抢 2 份紧俏帖
# --------------------------------------------------------------------------- #
def group_b() -> None:
    print("\n=== B. 散帖争用并发确认（5 抢 2，败单整单退回）===")
    reset()
    book = make_book(["E", "E", "E", "E"], "B-散帖争用")
    sids = list(add_samples(book, [(f"B{i}", 1) for i in range(1, 6)]).values())
    x = add_pile(book, "X-23", 2, ["E", "E"], 2)  # 紧俏：只有 2 份
    y = add_pile(book, "Y-4", 4, ["E"], 5)       # 充足：5 份

    def task(barrier: Barrier, idx: int):
        body = confirm_body(book, [(sids[idx], [x, y])])
        barrier.wait()
        return confirm(body)

    outcomes = run_concurrent(5, task)
    codes = [s for s, _ in outcomes]
    check("没有 5xx/网络错误", all(s in (200, 201, 409) for s in codes), outcomes)
    check("恰好 2 单确认成功(201)", codes.count(201) == 2, codes)
    check("恰好 3 单因余量冲突 409", codes.count(409) == 3, codes)
    check(
        "3 个冲突全部是 insufficient_stock 且指向 X 垛",
        all(
            b["detail"]["code"] == "insufficient_stock"
            and b["detail"]["shortages"][0]["pile_id"] == x
            for s, b in outcomes
            if s == 409
        ),
        [b.get("detail") for s, b in outcomes if s == 409],
    )

    piles = piles_map(book)
    check("紧俏 X 垛余量为 0", piles[x]["copies_remaining"] == 0, piles[x])
    check(
        "充足 Y 垛只被扣掉 2 份（败单没有误扣 Y）=> 余量 3",
        piles[y]["copies_remaining"] == 3,
        piles[y],
    )
    statuses = sample_status_map(book)
    check(
        "恰好 2 本样书被标记已领料",
        sum(v == "requisitioned" for v in statuses.values()) == 2,
        statuses,
    )
    check("服务端恰好 2 张领料单", order_count(book) == 2, order_count(book))

    status, mov = request("GET", f"/api/piles/{y}/movements")
    check(
        "Y 垛流水只有 2 条扣料且余额为 3",
        status == 200
        and len(mov["movements"]) == 2
        and all(m["delta"] == -1 for m in mov["movements"])
        and mov["pile"]["copies_remaining"] == 3,
        mov,
    )


# --------------------------------------------------------------------------- #
# C. 交错确认：串行重叠单 + 并发重叠单 + 同单并发重发
# --------------------------------------------------------------------------- #
def group_c() -> None:
    print("\n=== C. 交错确认（样书重叠）与幂等并发重发 ===")
    reset()
    book = make_book(["E", "E"], "C-交错确认")
    codes = add_samples(book, [(f"C{i}", 0) for i in range(1, 11)])
    p = add_pile(book, "P-12", 1, ["E", "E"], 99)

    c1, c2, c3, c4 = (codes[f"C{i}"] for i in range(1, 5))
    o1 = confirm_body(book, [(c1, [p]), (c2, [p])])
    o2 = confirm_body(book, [(c2, [p]), (c3, [p])])
    o3 = confirm_body(book, [(c3, [p]), (c4, [p])])

    s1, b1 = confirm(o1)
    check("交错序列 O1 先确认 -> 201", s1 == 201, (s1, b1))
    s2, b2 = confirm(o2)
    check("O2 含已被 O1 领料的 C2 -> 409", s2 == 409, (s2, b2))
    check(
        "O2 错误码 samples_already_requisitioned",
        b2["detail"]["code"] == "samples_already_requisitioned",
        b2.get("detail"),
    )
    s3, b3 = confirm(o3)
    check("O3 与 O1 不共享样书 -> 201", s3 == 201, (s3, b3))
    s1r, b1r = confirm(o1)
    check("重发 O1 -> 200 原单", s1r == 200 and b1r["order"]["id"] == b1["order"]["id"], (s1r, b1r))
    piles = piles_map(book)
    check("P 垛仅按 O1(2本)+O3(2本) 扣 4 份 => 95", piles[p]["copies_remaining"] == 95, piles[p])
    statuses = sample_status_map(book)
    check(
        "C1..C4 最终全部已领料（O2 整单退回，C3/C4 由 O3 领走）",
        all(statuses[x] == "requisitioned" for x in (c1, c2, c3, c4)),
        statuses,
    )

    # 并发重叠单 O4=[C5,C6], O5=[C6,C7], O6=[C7,C8]
    c5, c6, c7, c8 = (codes[f"C{i}"] for i in range(5, 9))
    orders = {
        "O4": confirm_body(book, [(c5, [p]), (c6, [p])]),
        "O5": confirm_body(book, [(c6, [p]), (c7, [p])]),
        "O6": confirm_body(book, [(c7, [p]), (c8, [p])]),
    }

    def race_task(barrier: Barrier, idx: int):
        name = ["O4", "O5", "O6"][idx]
        barrier.wait()
        s, b = confirm(orders[name])
        return name, s, b

    race_outcomes = run_concurrent(3, race_task)
    winners = {name for name, s, _ in race_outcomes if s == 201}
    check(
        "并发交错只可能是 {O5} 或 {O4,O6} 成功（实际: %s）" % winners,
        winners == {"O5"} or winners == {"O4", "O6"},
        [(n, s, b.get("detail", {}).get("code") if isinstance(b, dict) else b) for n, s, b in race_outcomes],
    )
    check(
        "所有败因都是样书已领料（不是余量）",
        all(
            s == 409 and b["detail"]["code"] == "samples_already_requisitioned"
            for _, s, b in race_outcomes
            if s != 201
        ),
        race_outcomes,
    )
    winner_samples = {
        "O4": {c5, c6},
        "O5": {c6, c7},
        "O6": {c7, c8},
    }
    claimed_by_winners = set().union(*(winner_samples[w] for w in winners))
    statuses = sample_status_map(book)
    check(
        "并发后样书领料集合与胜单并集完全一致",
        {sid for sid, st in statuses.items() if st == "requisitioned" and sid in {c5,c6,c7,c8}}
        == claimed_by_winners,
        {"claimed_status": statuses, "winners": winners},
    )
    expected_remaining = 95 - 2 * len(winners)  # 每胜单 2 本
    check(
        "P 垛扣料数恰好等于胜单样书本数 => 余量 %d" % expected_remaining,
        piles_map(book)[p]["copies_remaining"] == expected_remaining,
        piles_map(book)[p]["copies_remaining"],
    )

    # 同一张单（带相同 request_id）5 连并发：只能建一次
    c9 = codes["C9"]
    body_dup = confirm_body(book, [(c9, [p])], request_id="idem-c9-001")

    def dup_task(barrier: Barrier, idx: int):
        barrier.wait()
        return confirm(body_dup)

    dup_outcomes = run_concurrent(5, dup_task)
    dup_status = [s for s, _ in dup_outcomes]
    dup_ids = {b["order"]["id"] for _, b in dup_outcomes if b and "order" in b}
    check("同 request_id 五连：恰好 1 个 201", dup_status.count(201) == 1, dup_status)
    check("同 request_id 五连：其余 4 个 200 重放", dup_status.count(200) == 4, dup_status)
    check("五连返回完全相同的领料单 ID", len(dup_ids) == 1, dup_ids)

    # 不带 request_id、完全相同方案 5 连并发：靠指纹唯一约束只建一次
    c10 = codes["C10"]
    body_fp = confirm_body(book, [(c10, [p])])

    def fp_task(barrier: Barrier, idx: int):
        barrier.wait()
        return confirm(body_fp)

    fp_outcomes = run_concurrent(5, fp_task)
    fp_status = [s for s, _ in fp_outcomes]
    fp_ids = {b["order"]["id"] for _, b in fp_outcomes if b and "order" in b}
    check("同方案指纹五连：恰好 1 个 201", fp_status.count(201) == 1, fp_status)
    check("同方案指纹五连：其余 4 个 200", fp_status.count(200) == 4, fp_status)
    check("指纹五连返回同一张单", len(fp_ids) == 1, fp_ids)
    check(
        "两次幂等风暴总共只再扣 2 份 => P 余量 %d" % (expected_remaining - 2),
        piles_map(book)[p]["copies_remaining"] == expected_remaining - 2,
        piles_map(book)[p]["copies_remaining"],
    )


# --------------------------------------------------------------------------- #
# D. 整单原子性：汇总余量不足时，够用的垛也不能扣
# --------------------------------------------------------------------------- #
def group_d() -> None:
    print("\n=== D. 整单退回原子性（一垛不足，另一垛不得预扣）===")
    reset()
    book = make_book(["E", "E", "E", "E"], "D-整单原子")
    sids = list(add_samples(book, [("D1", 1), ("D2", 1)]).values())
    x = add_pile(book, "X-23", 2, ["E", "E"], 2)  # 2 本都够
    y = add_pile(book, "Y-4", 4, ["E"], 1)        # 只够 1 本，本单要 2

    body = confirm_body(book, [(sids[0], [x, y]), (sids[1], [x, y])])
    status, resp = confirm(body)
    check("汇总余量不足 -> 409", status == 409, (status, resp))
    check(
        "错误码 insufficient_stock 且短缺垛为 Y(required=2,remaining=1)",
        resp["detail"]["code"] == "insufficient_stock"
        and any(
            sh["pile_id"] == y and sh["required"] == 2 and sh["remaining"] == 1
            for sh in resp["detail"]["shortages"]
        ),
        resp.get("detail"),
    )
    piles = piles_map(book)
    check("整单退回：X 垛维持 2（没有先扣）", piles[x]["copies_remaining"] == 2, piles[x])
    check("整单退回：Y 垛维持 1", piles[y]["copies_remaining"] == 1, piles[y])
    statuses = sample_status_map(book)
    check(
        "两本样书都仍是 available",
        all(v == "available" for v in statuses.values()),
        statuses,
    )
    check("没有生成任何领料单", order_count(book) == 0, order_count(book))


# --------------------------------------------------------------------------- #
# E. request_id 归属：换号重发、同号争用、首次失败可重试、重启后仍返回原单
# --------------------------------------------------------------------------- #
def group_e() -> None:
    print("\n=== E. request_id 归属（换号重发 / 同号争用 / 失败重试 / 重启）===")
    reset()
    book = make_book(["E", "E"], "E-请求号归属")
    smap = add_samples(book, [("E1", 0), ("E2", 0), ("E3", 0), ("E4", 0), ("E5", 0)])
    p = add_pile(book, "EP-12", 1, ["E", "E"], 5)

    e1, e2, e3, e4, e5 = (smap[f"E{i}"] for i in range(1, 6))
    plan_a = confirm_body(book, [(e1, [p])], request_id="E-R1")
    plan_b = confirm_body(book, [(e2, [p])])

    # 1) 首次带 E-R1 成功 -> 201，原始号落在单上。
    s, r1 = confirm(plan_a)
    check("E1 首次带 request_id 确认 -> 201", s == 201, (s, r1))
    order1 = r1["order"]
    check(
        "建单自带号保留在 request_id 且 request_ids 含原始号",
        order1["request_id"] == "E-R1" and order1["request_ids"] == ["E-R1"],
        order1,
    )

    # 2) 同一方案换个新号 E-R2 重发：拿到原单，且 E-R2 归原单、旧回执不变。
    s, r2 = confirm({**plan_a, "request_id": "E-R2"})
    check("换号 E-R2 重发同方案 -> 200 原单", s == 200 and r2["replayed"] is True, (s, r2))
    check(
        "换号重发返回同一张单 LL-0001",
        r2["order"]["id"] == order1["id"] and r2["order"]["order_no"] == "LL-0001",
        r2["order"],
    )
    check(
        "旧单原始 request_id 保持 E-R1，request_ids 追加 E-R2",
        r2["order"]["request_id"] == "E-R1"
        and r2["order"]["request_ids"] == ["E-R1", "E-R2"],
        r2["order"],
    )
    s, detail_old = request("GET", f"/api/requisitions/{order1['order_no']}")
    check(
        "旧单回执内容未被改写（仍是 E-R1，request_ids 含两号）",
        s == 200
        and detail_old["request_id"] == "E-R1"
        and detail_old["request_ids"] == ["E-R1", "E-R2"],
        detail_old if s != 200 else {"request_id": detail_old.get("request_id"),
                                     "request_ids": detail_old.get("request_ids")},
    )
    check(
        "换号重发没有二次扣料（P 仍为 4）",
        piles_map(book)[p]["copies_remaining"] == 4,
        piles_map(book)[p],
    )

    # 3) 再用已归属的号 E-R2 提交另一份方案 -> 409，库存/样书/流水保持原状。
    stock_before = {k: v["copies_remaining"] for k, v in piles_map(book).items()}
    status_before = sample_status_map(book)
    moves_before = request("GET", f"/api/piles/{p}/movements")[1]
    s, r3 = confirm({**plan_b, "request_id": "E-R2"})
    check("同号用于其他方案 -> 409", s == 409, (s, r3))
    check(
        "错误码 idempotency_key_conflict 且指向原单",
        r3["detail"]["code"] == "idempotency_key_conflict"
        and r3["detail"]["existing_order_no"] == "LL-0001",
        r3.get("detail"),
    )
    check("409 后散帖余量保持原状", {k: v["copies_remaining"] for k, v in piles_map(book).items()} == stock_before)
    check("409 后样书状态保持原状", sample_status_map(book) == status_before, sample_status_map(book))
    check(
        "409 后扣料流水保持原状（仍只有 1 条）",
        len(request("GET", f"/api/piles/{p}/movements")[1]["movements"]) == len(moves_before["movements"])
        == 1,
    )
    check("409 没有新建领料单（仍 1 张）", order_count(book) == 1, order_count(book))

    # 4) 同号同方案再次重发（E-R2 已归属）仍返回原单 200。
    s, r4 = confirm({**plan_a, "request_id": "E-R2"})
    check("E-R2 同方案再次重发 -> 200 原单", s == 200 and r4["order"]["id"] == order1["id"], (s, r4))

    # 5) 首次请求失败时该号不被占用：余量不足 409 后，同号可换方案重试。
    tight = add_pile(book, "ET-12", 1, ["E", "E"], 1)  # 仅 1 份，两本要 2 份
    s, failed = confirm(
        confirm_body(book, [(e4, [tight]), (e5, [tight])], request_id="E-RETRY")
    )
    check(
        "首次请求因余量不足失败 -> 409",
        s == 409 and failed["detail"]["code"] == "insufficient_stock",
        (s, failed),
    )
    check(
        "失败后该号尚未归属任何单（列表中无 E-RETRY）",
        not any(
            "E-RETRY" in (o.get("request_ids") or [])
            for o in request("GET", f"/api/requisitions?book_id={book}")[1]
        ),
    )
    check(
        "失败后余量与样书状态未被改动",
        piles_map(book)[tight]["copies_remaining"] == 1
        and sample_status_map(book)[e4] == "available",
    )
    s, retried = confirm(confirm_body(book, [(e4, [p])], request_id="E-RETRY"))
    check(
        "失败过的号 E-RETRY 可用于重试并成功 -> 201",
        s == 201 and retried["order"]["request_ids"] == ["E-RETRY"],
        (s, retried),
    )

    # 6) 从试配入口确认的单一并适用：
    #    先用无号方式从 /api/requisitions 确认一份“单本”方案（无号建单），
    #    再用 T-R1 认领该单；最后用 T-R1 提交另一份方案 -> 409。
    reset()
    book2 = make_book(["E", "E"], "E-试配入口")
    smap2 = add_samples(book2, [("T1", 0), ("T2", 0)])
    p2 = add_pile(book2, "TP-12", 1, ["E", "E"], 2)
    s, first = confirm(confirm_body(book2, [(smap2["T1"], [p2])]))
    check("无号方式确认单本方案 -> 201", s == 201, (s, first))
    first_order = first["order"]
    check(
        "无号建单原始 request_id 为空、请求号集合为空",
        first_order["request_id"] is None and first_order["request_ids"] == [],
        first_order,
    )
    s, claim = confirm(confirm_body(book2, [(smap2["T1"], [p2])], request_id="T-R1"))
    check(
        "该单可用号 T-R1 认领 -> 200 原单",
        s == 200 and claim["order"]["id"] == first_order["id"],
        (s, claim),
    )
    check(
        "认领后 request_ids 含新号，原始 request_id 仍为空",
        claim["order"]["request_id"] is None and claim["order"]["request_ids"] == ["T-R1"],
        claim["order"],
    )
    s, plan_conflict = confirm(
        {**confirm_body(book2, [(smap2["T2"], [p2])]), "request_id": "T-R1"}
    )
    check("已归属的号用于其他方案 -> 409", s == 409, (s, plan_conflict))

    # 6b) 从试配入口确认的单一并适用：plan 确认产生无号单，再被号认领。
    reset()
    book4 = make_book(["E", "E"], "E-试配确认")
    smap4 = add_samples(book4, [("P1", 0), ("P2", 0)])
    p4 = add_pile(book4, "P-12", 1, ["E", "E"], 2)
    plan4 = trial(book4)
    s, plan_order = confirm_plan(plan4["plan_id"])
    check("试配方案确认 -> 201", s == 201, (s, plan_order))
    plan_o = plan_order["order"]
    check("试配确认单无原始 request_id", plan_o["request_id"] is None, plan_o["request_id"])
    s, claim2 = confirm({**plan4["confirm_request"], "request_id": "T-R2"})
    check(
        "试配确认后可用号认领原单 -> 200 原单",
        s == 200 and claim2["order"]["id"] == plan_o["id"],
        (s, claim2),
    )
    check(
        "试配单 request_ids 含新号，原始 request_id 仍为空",
        claim2["order"]["request_id"] is None
        and claim2["order"]["request_ids"] == ["T-R2"],
        claim2["order"],
    )
    s, plan_conflict2 = confirm(
        {**confirm_body(book4, [(smap4["P2"], [p4])]), "request_id": "T-R2"}
    )
    check("试配单已归属的号用于其他方案 -> 409", s == 409, (s, plan_conflict2))

    # 7) 并发：同一个号 + 多个不同方案同时提交，只有首个成功的方案归该号。
    reset()
    book3 = make_book(["E", "E"], "E-同号并发")
    codes3 = add_samples(book3, [(f"R{i}", 0) for i in range(1, 7)])
    p3 = add_pile(book3, "RP-12", 1, ["E", "E"], 6)
    bodies = {
        code: confirm_body(book3, [(sid, [p3])], request_id="RACE-1")
        for code, sid in codes3.items()
    }
    names = list(bodies)

    def race_same_key(barrier: Barrier, idx: int):
        name = names[idx]
        barrier.wait()
        return name, confirm(bodies[name])

    outcomes = run_concurrent(len(names), race_same_key)
    winners = [(n, b) for n, (s, b) in outcomes if s == 201]
    check("同号并发不同方案：恰好 1 个 201", len(winners) == 1, [(n, s) for n, (s, _) in outcomes])
    check(
        "同号并发落败方 -> 409 idempotency_key_conflict",
        all(
            s == 409 and b["detail"]["code"] == "idempotency_key_conflict"
            for n, (s, b) in outcomes
            if s != 201
        ),
        [b.get("detail") for _, (s, b) in outcomes if s != 201],
    )
    win_name, win_body = winners[0]
    win_order = win_body["order"]
    check(
        "成功方案的单归 RACE-1",
        win_order["request_ids"] == ["RACE-1"] and win_order["request_id"] == "RACE-1",
        win_order,
    )
    win_sample = codes3[win_name]
    lose_samples = [sid for code, sid in codes3.items() if code != win_name]
    statuses = sample_status_map(book3)
    check("胜单样书已领料", statuses[win_sample] == "requisitioned", statuses)
    check(
        "落败方案样书仍 available（未误扣）",
        all(statuses[sid] == "available" for sid in lose_samples),
        statuses,
    )
    check("同号并发只扣 1 份 => P 余量 5", piles_map(book3)[p3]["copies_remaining"] == 5, piles_map(book3)[p3])
    check("同号并发只建 1 张单", order_count(book3) == 1, order_count(book3))
    # 用归属号重发胜方方案 -> 原单。
    s, replay = confirm(bodies[win_name])
    check("胜方方案用 RACE-1 重发 -> 200 原单", s == 200 and replay["order"]["id"] == win_order["id"], (s, replay))

    # 8) 重启后同号同方案仍返回原单：状态随库持久化，由 --restart-check 复核。
    print(
        "  SELFTEST_RESTART_STATE "
        + json.dumps(
            {
                "book_id": book3,
                "request_id": "RACE-1",
                "order_id": win_order["id"],
                "win_sample": win_sample,
                "pile_id": p3,
                "remaining": piles_map(book3)[p3]["copies_remaining"],
            }
        )
    )


def run_restart_check(state: dict[str, Any]) -> None:
    """服务在同一 SQLite 文件上重启后，用已归属号重发仍返回原单、不二次扣料。"""
    print("\n=== E8. 重启后同号同方案仍返回原单（复检）===")
    book = state["book_id"]
    order_id = state["order_id"]
    pile = state["pile_id"]
    expected_remaining = state["remaining"]
    # 重发此前并发胜出的方案体。
    plan = confirm_body(book, [(state["win_sample"], [pile])], request_id=state["request_id"])
    s, body = confirm(plan)
    check(
        "重启后同号同方案 -> 200 原单",
        s == 200 and body["replayed"] is True and body["order"]["id"] == order_id,
        (s, body),
    )
    check(
        f"重启后重发不二次扣料（余量仍为 {expected_remaining}）",
        piles_map(book)[pile]["copies_remaining"] == expected_remaining,
        piles_map(book)[pile],
    )


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--restart-check":
        state = json.loads(sys.argv[2])
        wait_health()
        run_restart_check(state)
        print("\n" + "=" * 68)
        print(f"通过 {_passed} 项，失败 {_failed} 项")
        if _failed:
            print("失败项:")
            for name in _failures:
                print(f"  - {name}")
            return 1
        print("重启复检通过 ✅")
        print("SELFTEST_SUMMARY " + json.dumps({"passed": _passed, "failed": _failed}))
        return 0

    print(f"目标服务: {BASE}")
    wait_health()
    group_a()
    group_b()
    group_c()
    group_d()
    group_e()

    print("\n" + "=" * 68)
    print(f"通过 {_passed} 项，失败 {_failed} 项")
    if _failed:
        print("失败项:")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print("全部自测通过 ✅")
    print("SELFTEST_SUMMARY " + json.dumps({"passed": _passed, "failed": _failed}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
