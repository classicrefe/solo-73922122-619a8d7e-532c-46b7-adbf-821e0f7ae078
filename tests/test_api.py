"""FastAPI 全流程测试：转换、封存、补报更正、取数、失败保原表。"""

SIM30 = [3, 0, 7, 11, 25, 1, 0, 9, 4, 40,
         2, 6, 8, 0, 13, 5, 5, 100, 0, 2,
         7, 7, 3, 0, 1, 9, 12, 30, 0, 6]


def test_convert_creates_version_and_table(client):
    resp = client.post("/convert", json={
        "year": 2026, "month": 1, "sim_rain": SIM30, "approved": {1: 5}
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version"] == 1
    assert body["calendar_days"] == 31
    assert body["sim_total"] == sum(SIM30)
    assert body["table_total"] == sum(SIM30)
    assert len(body["days"]) == 31
    first = body["days"][0]
    assert first["day"] == 1 and first["amount"] == 5
    assert first["source"] == "approved"
    assert all(d["amount"] >= 0 for d in body["days"])
    # 任意连续区间误差 < 1（用返回的精确份额复核）
    from fractions import Fraction
    exact = [Fraction(d["exact"]) for d in body["days"]]
    rounded = [d["amount"] for d in body["days"]]
    for a in range(31):
        for b in range(a, 31):
            assert abs(sum(rounded[a:b + 1]) - sum(exact[a:b + 1])) < 1


def test_invalid_payload_422(client):
    resp = client.post("/convert", json={
        "year": 2026, "month": 1, "sim_rain": [1] * 29
    })
    assert resp.status_code == 422


def test_infeasible_keeps_old_table(client):
    r1 = client.post("/convert", json={
        "year": 2026, "month": 1, "sim_rain": SIM30
    })
    assert r1.status_code == 201
    bad_sim = [0] * 30
    bad_sim[29] = 100
    r2 = client.post("/convert", json={
        "year": 2026, "month": 1, "sim_rain": bad_sim,
        "approved": {30: 40, 31: 40}
    })
    assert r2.status_code == 422
    # 原表继续供应，现行版号不变
    cur = client.get("/table").json()
    assert cur["version"] == 1 and cur["table_total"] == sum(SIM30)


def test_seal_then_recompute_preserves_sealed_values(client):
    client.post("/convert", json={
        "year": 2026, "month": 1, "sim_rain": SIM30, "approved": {2: 10}
    })
    seal = client.post("/seal", json={"through_day": 10})
    assert seal.status_code == 200
    sealed_snapshot = seal.json()["days"]
    sealed_amount = seal.json()["sealed_amount"]

    # 更正已封存日 -> 拒绝，且现行版本不变
    denied = client.post("/recompute", json={"approved": {3: 99}})
    assert denied.status_code == 422
    assert "封存" in denied.json()["detail"]
    assert client.get("/versions").json()["current_version"] == 1

    # 对未封存日补报 -> 新版，封存值原样保留
    ok = client.post("/recompute", json={"approved": {20: 15, 31: 8}})
    assert ok.status_code == 201, ok.text
    body = ok.json()
    assert body["version"] == 2
    assert body["sealed_through"] == 10
    new_prefix = [d for d in body["days"] if d["day"] <= 10]
    assert [(d["day"], d["amount"]) for d in new_prefix] == \
           [(d["day"], d["amount"]) for d in sealed_snapshot]
    assert sum(d["amount"] for d in new_prefix) == sealed_amount
    by_day = {d["day"]: d for d in body["days"]}
    assert by_day[20]["amount"] == 15 and by_day[20]["source"] == "approved"
    assert by_day[31]["amount"] == 8
    # 封存日来源标签保持基版本义（估算日封存后仍是 estimated，仅数值冻结）
    v1_by_day = {d["day"]: d for d in
                 client.get("/table", params={"version": 1}).json()["days"]}
    for d in range(1, 11):
        assert by_day[d]["source"] == v1_by_day[d]["source"]
    # 整月守恒仍成立
    assert body["table_total"] == body["sim_total"] == sum(SIM30)


def test_recompute_with_sim_correction_keeps_seal(client):
    client.post("/convert", json={
        "year": 2026, "month": 4, "sim_rain": SIM30
    })
    client.post("/seal", json={"through_day": 5})
    v1 = client.get("/table").json()
    sealed_v1 = [(d["day"], d["amount"]) for d in v1["days"][:5]]

    new_sim = SIM30.copy()
    new_sim[0] += 50  # 更正模拟月总量
    resp = client.post("/recompute", json={"sim_rain": new_sim})
    assert resp.status_code == 201
    body = resp.json()
    assert body["sim_total"] == sum(new_sim)
    assert body["table_total"] == sum(new_sim)
    assert [(d["day"], d["amount"]) for d in body["days"][:5]] == sealed_v1


def test_seal_beyond_month_rejected(client):
    client.post("/convert", json={
        "year": 2026, "month": 2, "sim_rain": SIM30
    })  # 28 天
    resp = client.post("/seal", json={"through_day": 29})
    assert resp.status_code == 422


def test_read_filters_and_cumulative(client):
    client.post("/convert", json={
        "year": 2026, "month": 1, "sim_rain": SIM30, "approved": {1: 5}
    })
    appr = client.get("/table", params={"source": "approved"}).json()
    assert all(d["source"] == "approved" for d in appr["days"])
    assert [d["day"] for d in appr["days"]] == [1]

    cum = client.get("/cumulative", params={"through_day": 7}).json()
    table = client.get("/table").json()["days"]
    assert cum["cumulative_amount"] == sum(d["amount"] for d in table[:7])
    assert cum["is_sealed"] is False

    client.post("/seal", json={"through_day": 10})
    cum2 = client.get("/cumulative", params={"through_day": 8}).json()
    assert cum2["is_sealed"] is True

    versions = client.get("/versions").json()
    assert versions["current_version"] == 1
    assert versions["versions"][0]["sealed_through"] == 10


def test_recompute_before_convert_conflict(client):
    assert client.post("/recompute", json={"approved": {1: 1}}).status_code == 409
    assert client.get("/table").status_code == 404
