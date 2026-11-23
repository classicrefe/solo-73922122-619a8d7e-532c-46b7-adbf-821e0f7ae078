"""核查/确认 API 测试：快照一致性、见证表、乐观并发与退回语义。"""

SIM30 = [3, 0, 7, 11, 25, 1, 0, 9, 4, 40,
         2, 6, 8, 0, 13, 5, 5, 100, 0, 2,
         7, 7, 3, 0, 1, 9, 12, 30, 0, 6]


def _convert(client, month=1, approved=None):
    resp = client.post("/convert", json={
        "year": 2026, "month": month, "sim_rain": SIM30,
        "approved": approved or {},
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def _find_extension(client, sealed, grid_step=3):
    table = client.get("/table").json()
    d_count = table["calendar_days"]
    for n in range(1, d_count):
        for t in range(0, table["sim_total"] + 5, grid_step):
            r = client.post("/audit/check", json={"n": n, "threshold": t})
            assert r.status_code == 200, r.text
            body = r.json()
            if body["needs_extension"]:
                return body
    return None


def test_audit_check_before_convert_conflict(client):
    assert client.post("/audit/check",
                       json={"n": 1, "threshold": 0}).status_code == 409
    assert client.post("/audit/confirm", json={
        "n": 1, "threshold": 0, "version": 1,
        "original_sealed_through": 0}).status_code == 409


def test_audit_check_validates_n_range(client):
    _convert(client)
    bad = client.post("/audit/check", json={"n": 32, "threshold": 0})
    assert bad.status_code == 422
    neg = client.post("/audit/check", json={"n": 1, "threshold": -1})
    assert neg.status_code == 422


def test_audit_check_full_month_window_needs_no_extension(client):
    table = _convert(client)
    d_count = table["calendar_days"]
    r = client.post("/audit/check",
                    json={"n": d_count, "threshold": 100})
    body = r.json()
    assert r.status_code == 200
    assert body["version"] == 1
    assert body["original_sealed_through"] == 0
    assert body["needs_extension"] is False
    assert body["suggested_sealed_through"] == 0
    assert body["witness"] is None
    assert body["candidate_count"] >= 1
    assert body["unique_count"] == body["current_count"]


def test_audit_check_extension_returns_witness_tables(client):
    _convert(client)
    client.post("/seal", json={"through_day": 2})
    body = _find_extension(client, sealed=2)
    assert body is not None, "应能找到需要延长封存的参数组合"
    assert body["needs_extension"] is True
    assert body["suggested_sealed_through"] > body["original_sealed_through"] == 2
    w = body["witness"]
    assert w["sealed_through"] == body["suggested_sealed_through"] - 1
    assert w["count_a"] != w["count_b"]
    assert len(w["table_a"]) == len(w["table_b"]) == body["calendar_days"]
    # 见证表前 2 天（已封存）与现行表一致
    current = client.get("/table").json()["days"]
    for table in (w["table_a"], w["table_b"]):
        for d in range(2):
            assert table[d] == current[d]["amount"]
        assert all(x >= 0 for x in table)
        assert sum(table) == sum(SIM30)


def test_confirm_extends_seal_and_is_idempotent(client):
    _convert(client)
    client.post("/seal", json={"through_day": 2})
    plan = _find_extension(client, sealed=2)
    assert plan is not None

    r = client.post("/audit/confirm", json={
        "n": plan["n"], "threshold": plan["threshold"],
        "version": plan["version"],
        "original_sealed_through": plan["original_sealed_through"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["extended"] is True
    assert body["sealed_through"] == plan["suggested_sealed_through"]
    assert body["unique_count"] == plan["unique_count"]
    assert body["sealed_at"] is not None

    versions = client.get("/versions").json()
    assert versions["versions"][0]["sealed_through"] == \
        plan["suggested_sealed_through"]

    # 再次确认：截止日已推进，按原截止日确认 -> 409 退回，不新增封存
    r2 = client.post("/audit/confirm", json={
        "n": plan["n"], "threshold": plan["threshold"],
        "version": plan["version"],
        "original_sealed_through": plan["original_sealed_through"],
    })
    assert r2.status_code == 409
    assert "截止日" in r2.json()["detail"]

    # 用新快照确认：已足够，直接返回次数且不再封存
    recheck = client.post("/audit/check",
                          json={"n": plan["n"], "threshold": plan["threshold"]}).json()
    assert recheck["needs_extension"] is False
    r3 = client.post("/audit/confirm", json={
        "n": recheck["n"], "threshold": recheck["threshold"],
        "version": recheck["version"],
        "original_sealed_through": recheck["original_sealed_through"],
    })
    assert r3.status_code == 200
    assert r3.json()["extended"] is False
    assert r3.json()["unique_count"] == plan["unique_count"]


def test_confirm_rejects_stale_version_after_recompute(client):
    _convert(client, approved={20: 7})
    plan = client.post("/audit/check",
                       json={"n": 2, "threshold": 5}).json()
    # 求解期间发生补报 -> 新版号
    rc = client.post("/recompute", json={"approved": {25: 11}})
    assert rc.status_code == 201
    assert rc.json()["version"] == 2

    r = client.post("/audit/confirm", json={
        "n": plan["n"], "threshold": plan["threshold"],
        "version": plan["version"],
        "original_sealed_through": plan["original_sealed_through"],
    })
    assert r.status_code == 409
    assert "版号" in r.json()["detail"]
    # 未新增封存
    assert client.get("/versions").json()["versions"][-1]["sealed_through"] == 0


def test_confirm_rejects_stale_cutoff_after_seal(client):
    _convert(client)
    plan = _find_extension(client, sealed=0, grid_step=11)
    assert plan is not None
    # 求解期间另有封存推进了截止日
    assert client.post("/seal",
                       json={"through_day": plan["suggested_sealed_through"]}
                       ).status_code == 200
    r = client.post("/audit/confirm", json={
        "n": plan["n"], "threshold": plan["threshold"],
        "version": plan["version"],
        "original_sealed_through": plan["original_sealed_through"],
    })
    assert r.status_code == 409
    assert "截止日" in r.json()["detail"]


def test_check_uses_committed_snapshot_after_recompute(client):
    _convert(client)
    client.post("/seal", json={"through_day": 8})
    r1 = client.post("/audit/check", json={"n": 3, "threshold": 20}).json()
    client.post("/recompute", json={"approved": {20: 15, 31: 8}})
    r2 = client.post("/audit/check", json={"n": 3, "threshold": 20}).json()
    # 版号随已提交状态推进，截止日由新版本继承
    assert r1["version"] == 1 and r2["version"] == 2
    assert r2["original_sealed_through"] == 8
    assert r2["calendar_days"] == r1["calendar_days"] == 31


def test_witness_tables_satisfy_interval_error_via_exact_shares(client):
    from fractions import Fraction
    _convert(client)
    plan = _find_extension(client, sealed=0, grid_step=5)
    assert plan is not None
    table_out = client.get("/table").json()
    exact = [Fraction(d["exact"]) for d in table_out["days"]]
    for key in ("table_a", "table_b"):
        tab = plan["witness"][key]
        n = len(tab)
        for a in range(n):
            for b in range(a, n):
                assert abs(sum(tab[a:b + 1]) - sum(exact[a:b + 1])) < 1
