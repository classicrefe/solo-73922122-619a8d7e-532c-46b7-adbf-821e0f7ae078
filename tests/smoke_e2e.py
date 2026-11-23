"""端到端冒烟：转换 -> 取数 -> 封存 -> 补报更正 -> 无解保原表。"""

import json
import os
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")


def call(method, path, payload=None, expect=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            body = json.loads(r.read())
            code = r.status
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        code = e.code
    if expect is not None:
        assert code == expect, f"{method} {path} -> {code}: {body}"
    return code, body


SIM = [3, 0, 7, 11, 25, 1, 0, 9, 4, 40,
       2, 6, 8, 0, 13, 5, 5, 100, 0, 2,
       7, 7, 3, 0, 1, 9, 12, 30, 0, 6]
print("sim_total =", sum(SIM))

# 1) 初始转换（2026-01，31 天，核准第 1 日=5）
c, t1 = call("POST", "/convert",
             {"year": 2026, "month": 1, "sim_rain": SIM, "approved": {"1": 5}}, 201)
print("v1: version=%s days=%d table_total=%d sealed=%s"
      % (t1["version"], len(t1["days"]), t1["table_total"], t1["sealed_through"]))
assert t1["table_total"] == sum(SIM)
assert t1["days"][0] == {"day": 1, "amount": 5, "source": "approved",
                         "exact": t1["days"][0]["exact"]}

# 2) 取数区分来源
c, appr = call("GET", "/table?source=approved", expect=200)
print("approved days:", [(d["day"], d["amount"]) for d in appr["days"]])

# 3) 封存到第 10 日
c, seal = call("POST", "/seal", {"through_day": 10}, 200)
print("seal:", {k: v for k, v in seal.items() if k != "days"})
sealed_snapshot = [(d["day"], d["amount"]) for d in seal["days"]]

# 4) 更正已封存日 -> 422，现行版仍是 v1
c, denied = call("POST", "/recompute", {"approved": {"3": 99}}, 422)
print("recompute sealed-day ->", c, denied["detail"][:40], "...")
c, vs = call("GET", "/versions", expect=200)
assert vs["current_version"] == t1["version"]

# 5) 未封存日补报 -> v2，封存值不变
c, t2 = call("POST", "/recompute",
             {"approved": {"20": 15, "31": 8}}, 201)
kept = [(d["day"], d["amount"]) for d in t2["days"][:10]]
print("v2: version=%s sealed_through=%d prefix_kept=%s total=%d"
      % (t2["version"], t2["sealed_through"], kept == sealed_snapshot,
         t2["table_total"]))
assert kept == sealed_snapshot
assert t2["table_total"] == sum(SIM)
by_day = {d["day"]: d for d in t2["days"]}
assert by_day[20]["amount"] == 15 and by_day[31]["amount"] == 8

# 6) 无解请求（核准超量）-> 422，原表 v2 继续供应
c, infeasible = call("POST", "/convert",
                     {"year": 2026, "month": 1, "sim_rain": SIM,
                      "approved": {"1": 99999}}, 422)
print("infeasible ->", c, infeasible["detail"][:40], "...")
c, cur = call("GET", "/table", expect=200)
assert cur["version"] == t2["version"] and cur["table_total"] == sum(SIM)

# 7) 累计查询 + 封存标记
c, cum = call("GET", "/cumulative?through_day=10", expect=200)
print("cumulative@10:", cum["cumulative_amount"], "sealed=", cum["is_sealed"])
assert cum["is_sealed"] is True

print("\nSMOKE OK")
