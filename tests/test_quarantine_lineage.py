"""谱系隔离：沿已提交且未撤销批次的 source→destination 边扩展孔位隔离。"""

import threading

from app import ledger


def register(client, well, sample, volume):
    resp = client.post("/wells", json={"well": well, "sample": sample, "volume": volume})
    assert resp.status_code == 201, resp.text
    return resp.json()


def batch(client, request_key, operations):
    resp = client.post(
        "/batches", json={"request_key": request_key, "operations": operations}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["batch_id"]


def lineage(client, seeds, case_id="CASE-L", reason="谱系污染排查", request_key="lq-1"):
    return client.post(
        "/quarantines/lineage",
        json={
            "request_key": request_key,
            "case_id": case_id,
            "reason": reason,
            "seeds": seeds,
        },
    )


def quarantine(client, wells, case_id="CASE-N", reason="普通隔离", request_key="q-1"):
    return client.post(
        "/quarantines",
        json={
            "request_key": request_key,
            "case_id": case_id,
            "reason": reason,
            "wells": wells,
        },
    )


def release(client, wells, case_id, request_key):
    return client.post(
        "/quarantines/releases",
        json={"request_key": request_key, "case_id": case_id, "wells": wells},
    )


def reverse(client, batch_id, request_key):
    return client.post(
        f"/batches/{batch_id}/reversals", json={"request_key": request_key}
    )


def wells_by_well(body):
    return {w["well"]: w for w in body["wells"]}


# ------------------------------------------------ 闭包与路径


def test_lineage_chain_propagates_with_paths(client):
    register(client, "A1", "S1", "100")
    b1 = batch(client, "lb-1", [{"source": "A1", "destination": "B1", "volume": "40"}])
    b2 = batch(client, "lb-2", [{"source": "B1", "destination": "C1", "volume": "10"}])

    resp = lineage(client, ["A1"], case_id="CASE-2026-201", reason="上游污染，沿谱系隔离")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["replayed"] is False
    assert body["case_id"] == "CASE-2026-201"
    assert body["reason"] == "上游污染，沿谱系隔离"
    assert body["seeds"] == ["A1"]
    assert [w["well"] for w in body["wells"]] == ["A1", "B1", "C1"]
    wells = wells_by_well(body)
    assert wells["A1"]["distance"] == 0
    assert wells["A1"]["path"] == ["A1"]
    assert wells["A1"]["hops"] == []
    assert wells["B1"]["distance"] == 1
    assert wells["B1"]["path"] == ["A1", "B1"]
    assert wells["B1"]["hops"] == [
        {"batch_id": b1, "step": 1, "source": "A1", "destination": "B1", "volume": "40"}
    ]
    assert wells["C1"]["distance"] == 2
    assert wells["C1"]["path"] == ["A1", "B1", "C1"]
    assert [h["batch_id"] for h in wells["C1"]["hops"]] == [b1, b2]
    assert wells["C1"]["hops"][1]["volume"] == "10"
    assert all(w["status"] == "quarantined" for w in body["wells"])
    assert all(w["released_at"] is None for w in body["wells"])

    # 液体不变；既有查询接口可见隔离信息，申请记录进入普通隔离列表
    assert client.get("/wells/C1").json()["volume"] == "10"
    quar = client.get("/wells/C1").json()["quarantine"]
    assert quar["case_id"] == "CASE-2026-201"
    assert quar["reason"] == "上游污染，沿谱系隔离"
    listing = client.get("/quarantines").json()["quarantines"]
    assert [q["quarantine_id"] for q in listing] == [body["quarantine_id"]]


def test_lineage_covers_seed_and_descendants_only(client):
    register(client, "A1", "S1", "100")
    batch(client, "lb-1", [{"source": "A1", "destination": "B1", "volume": "40"}])
    batch(client, "lb-2", [{"source": "B1", "destination": "C1", "volume": "10"}])

    resp = lineage(client, ["B1"], request_key="lq-mid")
    assert resp.status_code == 201, resp.text
    assert [w["well"] for w in resp.json()["wells"]] == ["B1", "C1"]
    # 上游孔不受影响
    assert client.get("/wells/A1").json()["quarantine"] is None


def test_lineage_cycle_counted_once(client):
    register(client, "A1", "S1", "100")
    batch(client, "lb-1", [{"source": "A1", "destination": "B1", "volume": "40"}])
    batch(client, "lb-2", [{"source": "B1", "destination": "A1", "volume": "10"}])

    resp = lineage(client, ["A1"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # 循环只计一次：每孔恰出现一次，种子路径不绕环
    assert [w["well"] for w in body["wells"]] == ["A1", "B1"]
    wells = wells_by_well(body)
    assert wells["A1"]["distance"] == 0
    assert wells["A1"]["path"] == ["A1"]
    assert wells["B1"]["path"] == ["A1", "B1"]


def test_lineage_shortest_path_lexicographic_tie_break(client):
    register(client, "A1", "S1", "100")
    batch(client, "lb-1", [{"source": "A1", "destination": "C1", "volume": "30"}])
    b2 = batch(client, "lb-2", [{"source": "A1", "destination": "B1", "volume": "30"}])
    batch(client, "lb-3", [{"source": "C1", "destination": "D1", "volume": "10"}])
    b4 = batch(client, "lb-4", [{"source": "B1", "destination": "D1", "volume": "10"}])

    resp = lineage(client, ["A1"])
    assert resp.status_code == 201, resp.text
    wells = wells_by_well(resp.json())
    # D1 有两条最短路径（经 B1 / 经 C1，均 2 跳）：取孔位序列字典序最小者
    assert wells["D1"]["distance"] == 2
    assert wells["D1"]["path"] == ["A1", "B1", "D1"]
    assert [h["batch_id"] for h in wells["D1"]["hops"]] == [b2, b4]


def test_lineage_multi_seed_shortest_and_lexicographic(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "50")
    batch(client, "lb-1", [{"source": "A2", "destination": "B1", "volume": "10"}])
    batch(client, "lb-2", [{"source": "A1", "destination": "B1", "volume": "10"}])
    batch(client, "lb-3", [{"source": "A1", "destination": "C1", "volume": "10"}])
    batch(client, "lb-4", [{"source": "C1", "destination": "B1", "volume": "5"}])

    resp = lineage(client, ["a2", "A1"])  # 大小写与顺序归一化
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["seeds"] == ["A1", "A2"]
    wells = wells_by_well(body)
    # B1 从两个种子各有一条 1 跳路径：取字典序最小（经 A1）；
    # 更长的 A1->C1->B1（2 跳）不参与最短判定
    assert wells["B1"]["distance"] == 1
    assert wells["B1"]["path"] == ["A1", "B1"]
    assert wells["C1"]["path"] == ["A1", "C1"]


def test_lineage_canonical_edge_between_same_wells(client):
    register(client, "A1", "S1", "100")
    b1 = batch(client, "lb-1", [{"source": "A1", "destination": "B1", "volume": "10"}])
    batch(client, "lb-2", [{"source": "A1", "destination": "B1", "volume": "5"}])

    resp = lineage(client, ["A1"])
    assert resp.status_code == 201, resp.text
    wells = wells_by_well(resp.json())
    # 同一对孔之间取 (batch_id, step) 最小的边作为路径明细
    assert wells["B1"]["hops"] == [
        {"batch_id": b1, "step": 1, "source": "A1", "destination": "B1", "volume": "10"}
    ]


def test_lineage_multiple_seeds_union_of_closures(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "50")
    batch(client, "lb-1", [{"source": "A1", "destination": "B1", "volume": "10"}])
    batch(client, "lb-2", [{"source": "A2", "destination": "B2", "volume": "10"}])

    resp = lineage(client, ["A1", "A2"])
    assert resp.status_code == 201, resp.text
    wells = wells_by_well(resp.json())
    assert set(wells) == {"A1", "A2", "B1", "B2"}
    assert wells["B1"]["path"] == ["A1", "B1"]
    assert wells["B2"]["path"] == ["A2", "B2"]


def test_lineage_excludes_reversed_and_failed_batch_edges(client):
    register(client, "A1", "S1", "100")
    batch(client, "lb-1", [{"source": "A1", "destination": "B1", "volume": "40"}])
    b2 = batch(client, "lb-2", [{"source": "B1", "destination": "C1", "volume": "10"}])
    batch(client, "lb-3", [{"source": "A1", "destination": "D1", "volume": "10"}])
    # 撤销 B1->C1（其孔上无后续事件）：该边不再计入谱系，C1 随撤销删除
    assert reverse(client, b2, "rev-2").status_code == 201
    assert client.get("/wells/C1").status_code == 404
    # 失败批次不产生边
    resp = client.post(
        "/batches",
        json={
            "request_key": "lb-fail",
            "operations": [{"source": "A1", "destination": "E1", "volume": "999"}],
        },
    )
    assert resp.status_code == 409

    resp = lineage(client, ["A1"])
    assert resp.status_code == 201, resp.text
    assert [w["well"] for w in resp.json()["wells"]] == ["A1", "B1", "D1"]


# ------------------------------------------------ 原子拒绝


def test_lineage_unregistered_seed_is_404_and_no_partial_state(client):
    register(client, "A1", "S1", "100")
    batch(client, "lb-1", [{"source": "A1", "destination": "B1", "volume": "10"}])

    resp = lineage(client, ["A1", "Z9"])
    assert resp.status_code == 404, resp.text
    # 整次拒绝：无任何隔离、无申请记录、request_key 未消耗
    assert client.get("/quarantines").json()["quarantines"] == []
    assert client.get("/wells/A1").json()["quarantine"] is None
    assert client.get("/wells/B1").json()["quarantine"] is None
    assert lineage(client, ["A1"]).status_code == 201


def test_lineage_target_already_quarantined_rejects_atomically(client):
    register(client, "A1", "S1", "100")
    batch(client, "lb-1", [{"source": "A1", "destination": "B1", "volume": "40"}])
    batch(client, "lb-2", [{"source": "B1", "destination": "C1", "volume": "10"}])
    assert quarantine(client, ["C1"], case_id="CASE-X", request_key="q-x").status_code == 201

    resp = lineage(client, ["A1"], case_id="CASE-L", request_key="lq-1")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "already_quarantined"
    assert detail["conflicting_wells"] == [{"well": "C1", "case_id": "CASE-X"}]
    # 不留部分状态：A1、B1 未隔离，request_key 未消耗
    assert client.get("/wells/A1").json()["quarantine"] is None
    assert client.get("/wells/B1").json()["quarantine"] is None
    assert len(client.get("/quarantines").json()["quarantines"]) == 1

    # 解除冲突源后同键重试成功
    assert release(client, ["C1"], "CASE-X", "rel-x").status_code == 201
    resp = lineage(client, ["A1"], case_id="CASE-L", request_key="lq-1")
    assert resp.status_code == 201, resp.text
    assert [w["well"] for w in resp.json()["wells"]] == ["A1", "B1", "C1"]


def test_lineage_blocked_by_active_reservation_on_descendant(client):
    register(client, "A1", "S1", "100")
    batch(client, "lb-1", [{"source": "A1", "destination": "B1", "volume": "40"}])
    resp = client.post(
        "/reservations",
        json={"request_key": "rv-1", "items": [{"well": "B1", "volume": "10"}]},
    )
    assert resp.status_code == 201, resp.text

    resp = lineage(client, ["A1"])
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "reservations_active"
    assert [b["well"] for b in detail["blocking_wells"]] == ["B1"]
    assert detail["blocking_wells"][0]["reserved_volume"] == "10"
    # 不留部分状态
    assert client.get("/wells/A1").json()["quarantine"] is None
    assert client.get("/quarantines").json()["quarantines"] == []


def test_lineage_validation_rejected(client):
    register(client, "A1", "S1", "100")
    assert lineage(client, []).status_code == 422
    assert lineage(client, ["A1"], reason="").status_code == 422
    assert lineage(client, ["A1"], case_id="").status_code == 422
    assert lineage(client, ["not-a-well"]).status_code == 422


# ------------------------------------------------ 幂等与键冲突


def test_lineage_idempotent_replay(client):
    register(client, "A1", "S1", "100")
    batch(client, "lb-1", [{"source": "A1", "destination": "B1", "volume": "40"}])
    first = lineage(client, ["A1"], request_key="lq-idem")
    assert first.status_code == 201

    second = lineage(client, ["A1"], request_key="lq-idem")
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["quarantine_id"] == first.json()["quarantine_id"]
    assert second.json()["wells"] == first.json()["wells"]
    assert len(client.get("/quarantines").json()["quarantines"]) == 1


def test_lineage_replay_after_release_and_graph_growth_returns_first_closure(client):
    register(client, "A1", "S1", "100")
    batch(client, "lb-1", [{"source": "A1", "destination": "B1", "volume": "40"}])
    first = lineage(client, ["A1"], case_id="CASE-L", request_key="lq-frozen")
    assert first.status_code == 201
    assert release(client, ["A1", "B1"], "CASE-L", "rel-1").status_code == 201
    # 解除后谱系延伸：B1 -> C1
    batch(client, "lb-2", [{"source": "B1", "destination": "C1", "volume": "10"}])

    replay = lineage(client, ["A1"], case_id="CASE-L", request_key="lq-frozen")
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    # 闭包在申请时固定：仍返回首次结果（不含 C1），且不会重新隔离
    assert [w["well"] for w in replay.json()["wells"]] == ["A1", "B1"]
    assert [w["status"] for w in replay.json()["wells"]] == ["released", "released"]
    assert client.get("/wells/A1").json()["quarantine"] is None
    assert client.get("/wells/C1").json()["quarantine"] is None
    assert len(client.get("/quarantines").json()["quarantines"]) == 1


def test_lineage_same_key_other_target_conflicts(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "50")
    batch(client, "lb-1", [{"source": "A1", "destination": "B1", "volume": "10"}])
    assert lineage(client, ["A1"], case_id="CASE-L", request_key="lq-key").status_code == 201

    # 同键换种子
    resp = lineage(client, ["A2"], case_id="CASE-L", request_key="lq-key")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "request_key_conflict"
    assert detail["existing_case_id"] == "CASE-L"
    assert detail["existing_seeds"] == ["A1"]
    # 同键换案件
    resp = lineage(client, ["A1"], case_id="CASE-OTHER", request_key="lq-key")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "request_key_conflict"
    # 状态不变
    assert client.get("/wells/A2").json()["quarantine"] is None
    assert len(client.get("/quarantines").json()["quarantines"]) == 1


def test_lineage_key_collides_with_plain_quarantine(client):
    register(client, "A1", "S1", "100")
    register(client, "B1", "S2", "10")
    assert quarantine(client, ["A1"], case_id="CASE-N", request_key="shared").status_code == 201

    # 普通隔离占用的键不能用于谱系隔离（即使种子闭包恰好也是 A1）
    resp = lineage(client, ["A1"], case_id="CASE-N", request_key="shared")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "request_key_conflict"
    assert detail["existing_wells"] == ["A1"]

    # 反向：谱系隔离占用的键也不能被普通隔离换目标复用
    assert lineage(client, ["B1"], case_id="CASE-L", request_key="lq-shared").status_code == 201
    resp = quarantine(client, ["B1", "B2"], case_id="CASE-L", request_key="lq-shared")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "request_key_conflict"


# ------------------------------------------------ 隔离限制与解除


def test_lineage_quarantine_restricts_all_existing_operations(client):
    register(client, "A1", "S1", "100")
    b1 = batch(client, "lb-1", [{"source": "A1", "destination": "B1", "volume": "40"}])
    batch(client, "lb-2", [{"source": "B1", "destination": "C1", "volume": "10"}])
    # 计划创建于隔离之前：确认时应因隔离变化而失效
    plan = client.post(
        "/plans",
        json={
            "request_key": "lp-1",
            "operations": [{"source": "B1", "destination": "D1", "volume": "5"}],
        },
    )
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["plan_id"]

    assert lineage(client, ["A1"], case_id="CASE-L", request_key="lq-1").status_code == 201

    # 批次触及后代孔（来源或目标）
    resp = client.post(
        "/batches",
        json={
            "request_key": "lb-x",
            "operations": [{"source": "C1", "destination": "E1", "volume": "5"}],
        },
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "well_quarantined"
    assert {w["well"] for w in resp.json()["detail"]["quarantined_wells"]} == {"C1"}
    # 补液
    resp = client.post("/wells", json={"well": "B1", "sample": "S1", "volume": "5"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "well_quarantined"
    # 容量变更
    assert client.put("/wells/C1/capacity", json={"max_capacity": "50"}).status_code == 409
    # 预留
    resp = client.post(
        "/reservations",
        json={"request_key": "rv-x", "items": [{"well": "C1", "volume": "5"}]},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "well_quarantined"
    # 撤销触及隔离孔的批次
    resp = reverse(client, b1, "rev-x")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "well_quarantined"
    # 计划确认：快照失效（B1 被隔离），计划保持待确认
    resp = client.post(f"/plans/{plan_id}/confirmations", json={"confirm_key": "lc-1"})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "plan_stale"
    changed = {c["well"]: c["reasons"] for c in detail["changed_wells"]}
    assert "quarantined" in changed["B1"]
    assert client.get(f"/plans/{plan_id}").json()["status"] == "awaiting_confirmation"
    # 液体与批次均不变
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert client.get("/wells/B1").json()["volume"] == "30"
    assert client.get("/wells/C1").json()["volume"] == "10"
    assert client.get("/wells/E1").status_code == 404


def test_lineage_release_via_existing_endpoint(client):
    register(client, "A1", "S1", "100")
    batch(client, "lb-1", [{"source": "A1", "destination": "B1", "volume": "40"}])
    batch(client, "lb-2", [{"source": "B1", "destination": "C1", "volume": "10"}])
    assert lineage(client, ["A1"], case_id="CASE-L", request_key="lq-1").status_code == 201

    # 部分解除：B1 恢复可用，其余仍隔离
    assert release(client, ["B1"], "CASE-L", "rel-1").status_code == 201
    assert client.get("/wells/B1").json()["quarantine"] is None
    assert client.get("/wells/C1").json()["quarantine"]["case_id"] == "CASE-L"

    # 案件不符整体拒绝
    resp = release(client, ["A1", "C1"], "CASE-WRONG", "rel-2")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "release_mismatch"

    # 正确解除全部：申请记录保留并标记 released，孔位恢复可用
    assert release(client, ["A1", "C1"], "CASE-L", "rel-3").status_code == 201
    stored = client.get("/quarantines").json()["quarantines"][0]
    assert [w["status"] for w in stored["wells"]] == ["released", "released", "released"]
    assert register(client, "C1", "S9", "1")["volume"] == "11"

    # 重放仍返回申请时固定的谱系路径（含解除状态）
    replay = lineage(client, ["A1"], case_id="CASE-L", request_key="lq-1")
    assert replay.status_code == 200
    wells = wells_by_well(replay.json())
    assert wells["C1"]["path"] == ["A1", "B1", "C1"]
    assert wells["C1"]["status"] == "released"


def test_lineage_quarantine_does_not_change_liquid(client):
    register(client, "A1", "S1", "100")
    batch(client, "lb-1", [{"source": "A1", "destination": "B1", "volume": "40"}])
    assert lineage(client, ["A1"]).status_code == 201
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert client.get("/wells/B1").json()["volume"] == "40"


# ------------------------------------------------ 并发串行


class _Gate:
    """线程门闩：工作线程进入临界区后置位 entered，等待 release。"""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait_inside(self, timeout: float = 10.0) -> None:
        assert self.entered.wait(timeout), "工作线程未在超时内进入门闩"


def _run_in_thread(target):
    result = {}

    def runner():
        try:
            result["value"] = target()
        except Exception as exc:  # 在线程外重新抛出，附带原始堆栈信息
            result["error"] = exc

    thread = threading.Thread(target=runner)
    thread.start()
    return thread, result


def _join(thread, result, timeout: float = 30.0):
    thread.join(timeout)
    assert not thread.is_alive(), "工作线程超时未结束（可能死锁）"
    if "error" in result:
        raise result["error"]
    return result["value"]


def test_concurrent_batch_committed_first_is_included_in_closure(client, monkeypatch):
    """批次先持锁提交：谱系隔离在锁后计算闭包，包含新批次的边。"""
    register(client, "A1", "S1", "100")
    batch(client, "lb-0", [{"source": "A1", "destination": "B1", "volume": "40"}])
    gate = _Gate()
    original_execute = ledger.execute_batch

    def gated_execute(conn, *args, **kwargs):
        # 已在 BEGIN IMMEDIATE 之后执行：写锁在手
        gate.entered.set()
        assert gate.release.wait(10.0), "批次线程未被释放"
        return original_execute(conn, *args, **kwargs)

    monkeypatch.setattr(ledger, "execute_batch", gated_execute)

    # 批次先启动并持锁停在门闩内（B1 -> C1）
    batch_thread, batch_result = _run_in_thread(
        lambda: client.post(
            "/batches",
            json={
                "request_key": "lb-conc",
                "operations": [{"source": "B1", "destination": "C1", "volume": "10"}],
            },
        )
    )
    gate.wait_inside()

    # 谱系隔离在 BEGIN IMMEDIATE 处排队，不能越过批次的锁
    quar_thread, quar_result = _run_in_thread(
        lambda: lineage(client, ["A1"], request_key="lq-conc")
    )
    threading.Event().wait(0.3)
    assert quar_thread.is_alive(), "谱系隔离不应在批次持锁期间完成"

    gate.release.set()
    batch_resp = _join(batch_thread, batch_result)
    assert batch_resp.status_code == 201, batch_resp.text
    quar_resp = _join(quar_thread, quar_result)
    assert quar_resp.status_code == 201, quar_resp.text
    # 闭包在锁内计算：包含先提交批次的边
    assert [w["well"] for w in quar_resp.json()["wells"]] == ["A1", "B1", "C1"]


def test_concurrent_quarantine_first_blocks_batch(client, monkeypatch):
    """谱系隔离先持锁提交：触及后代孔的批次在锁后读到隔离状态并 409。"""
    register(client, "A1", "S1", "100")
    batch(client, "lb-0", [{"source": "A1", "destination": "B1", "volume": "40"}])
    gate = _Gate()
    original_apply = ledger.apply_lineage_quarantine

    def gated_apply(conn, *args, **kwargs):
        # 已在 BEGIN IMMEDIATE 之后执行：写锁在手
        gate.entered.set()
        assert gate.release.wait(10.0), "谱系隔离线程未被释放"
        return original_apply(conn, *args, **kwargs)

    monkeypatch.setattr(ledger, "apply_lineage_quarantine", gated_apply)

    quar_thread, quar_result = _run_in_thread(
        lambda: lineage(client, ["A1"], request_key="lq-conc2")
    )
    gate.wait_inside()

    # 批次在锁上等待
    batch_thread, batch_result = _run_in_thread(
        lambda: client.post(
            "/batches",
            json={
                "request_key": "lb-after",
                "operations": [{"source": "B1", "destination": "C1", "volume": "10"}],
            },
        )
    )
    threading.Event().wait(0.3)
    assert batch_thread.is_alive(), "批次不应在隔离持锁期间完成"

    gate.release.set()
    quar_resp = _join(quar_thread, quar_result)
    assert quar_resp.status_code == 201, quar_resp.text
    batch_resp = _join(batch_thread, batch_result)
    assert batch_resp.status_code == 409
    assert batch_resp.json()["detail"]["reason"] == "well_quarantined"
    assert client.get("/wells/C1").status_code == 404
