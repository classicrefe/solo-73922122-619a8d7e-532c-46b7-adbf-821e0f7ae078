# 气象数仓 · 模拟降雨转公历日表服务

单站单月的 30 个模拟日雨量（十分之一毫米，非负整数）按两种日历的重叠比例
拆成公历日表，支持核准日照录、版本封存、补报更正，并提供**月报连续降雨
窗口核查与确认入库**。FastAPI + SQLite，Docker Compose 一键启动。

## 1. 核心规则与算法

### 1.1 日历对齐与权重（`app/alloc.py::overlap_weights`）

- 模拟月固定 30 个模拟日，公历月按 `calendar.monthrange` 取 28~31 天；
  两种日历**月首、月末对齐**，时间轴归一化到同一月长。
- 模拟日 `i` 覆盖 `[i·L, (i+1)·L)`，公历日 `d` 覆盖 `[d, d+1)`，
  `L = 公历天数 / 30`；权重 = 重叠长度 / 模拟日长度，全部用
  `fractions.Fraction` 精确计算，无浮点误差。
- 每个模拟日拆出的权重和为 1；每个公历日收到的权重和为 `30/公历天数`。

### 1.2 核准与剩余量分摊（`app/alloc.py::allocate`）

- 核准公历日照录核准整数；剩余月量 `模拟总量 − 核准总量` 在未核准日之间
  按其重叠权重的精确份额比例分摊（权重为 0 而仍有剩余量时判无解）。
- 无解情形（422，不写库）：模拟值不是 30 个/含负数、核准日越界、
  核准总量超过模拟总量、剩余量无处分摊。

### 1.3 整数化：平移向下取整，保证任意连续区间误差 < 1

设 `E_d` 为前 d 天精确累计，整数日表取
`x_d = floor(E_d) − floor(E_(d−1))`（总量为整数，故 `E_n` 为整数）。
前缀误差 `e_d = floor(E_d) − E_d ∈ (−1, 0]` 且 `e_0 = e_n = 0`，
于是任意连续区间 `[a,b]` 的累计误差 `e_b − e_(a−1) ∈ (−1,1)`，
即**严格小于一个单位**。同时天然保证：日值非负整数、整月总量与模拟值
完全相等、核准日（精确份额为整数）取整后仍是原值。每次分配后
`_verify` 全枚举区间做终检。

## 2. 版本、封存与补报（`app/storage.py`、`app/main.py`）

- SQLite（WAL）四张表：`versions`（版本元数据）、`day_rows`
  （日值 + 来源 `approved/estimated` + 精确份额分子分母）、
  `seals`（封存区间）、`current_version`（现行版号，单行）。
- 所有写操作在 `BEGIN IMMEDIATE` 单事务内完成，异常即回滚；
  **无解或写入失败时现行表保持不变，继续供应原表**。
- `POST /seal` 凭现行版号封存月初到指定日（含），返回封存明细与累计量。
- `POST /recompute` 补报/更正：
  - 触及已封存日（`day ≤ sealed_through`）直接 422 拒绝；
  - 封存日以整数核准方式钉入重算，并在写入前逐日校验数值未被改动；
  - 封存只冻结数值，**来源标签按基版还原**（估算日封存后仍是 estimated）；
  - 新版继承基版全部封存记录并自动切为现行，旧版永久可查。

## 3. 月报核查：连续 N 天累计雨量达标窗口（`app/audit.py`）

### 3.1 候选日表的数学刻画

核查针对**同一已提交状态**：现行整数日值 `a_d`、取整前精确份额 `e_d`
（`Fraction`）、封存截止日与核准日在一次快照里同时读出。候选日表 `x` 必须：

1. 日值为非负整数；
2. 整月守恒：`sum(x) = sum(e)`（整数）；
3. 任意连续区间累计量与精确份额误差严格小于 1；
4. 封存日与核准日照录，逐值等于现行值。

记精确前缀 `S_k = Σ_{d≤k} e_d`、整数前缀 `X_k = Σ_{d≤k} x_d`，前缀误差
`d_k = X_k − S_k`。区间误差条件等价于任意两个前缀误差之差 `< 1`；又因
`d_D = 0`，每个 `X_k` 只能取 `floor(S_k)` 或 `ceil(S_k)`（整数 `S_k`
唯一）。对“低位 k、高位 j”配对，条件化为 `f_k < f_j`
（`f_k = S_k − floor(S_k)` 为小数部分）。因此可行表恰为**单临界值 c 的
平移取整族**：`f_k < c` 取 floor、`f_k ≥ c` 取 ceil。枚举只需在至多 D 个
不同小数部分断层处各取高/低两侧，共 O(D) 张候选表。

### 3.2 最少封存日求解（O(D³)，以月天数为规模的多项式）

- 钉死集合 = 原封存前缀 ∪ 全部核准日，额外封存 k 时把
  `s0+1..k` 按现行值追加钉死；
- 从原截止日 `s0` 起逐日推进 k，对所有与钉死值相容的候选表统计长度 N、
  累计量 ≥ 阈值 T 的窗口数（滑动求和 O(D)）；
- 首个次数集合只含一个值的 k 即为**建议封存日**，该值为**唯一次数**；
- 若 `k = s0` 已唯一则无需延长；否则给出**见证**：截止日为 `k−1` 时
  两张达标次数不同的候选表（均通过非负/守恒/区间误差/钉死值终检）。

### 3.3 接口与并发语义

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/audit/check` | 入参 `n`（1..当月天数）、`threshold`（非负整数）；返回版号、原截止日、现行次数、唯一次数、建议封存日、是否需延长、候选数与见证表 |
| POST | `/audit/confirm` | 凭原参数 + 版号 + 原截止日确认；需要延长时在单事务内条件封存，已足够则直接返回次数 |

- 求解期间补报/封存照常提交；确认在 `BEGIN IMMEDIATE` 事务内复核
  **现行版号与封存截止日**，任一变化即回滚返回 409，**不新增封存**；
- 条件封存只在目标日晚于现行截止日时写入，重复确认幂等安全。

## 4. API 一览（启动后见 `/docs` 在线试用）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/convert` | 初始转换，生成版本并切为现行（201） |
| POST | `/seal` | 封存现行版本月初到指定日 |
| POST | `/recompute` | 补报/更正重算（限未封存日，保留封存值） |
| POST | `/audit/check` | 月报核查：最少封存日、唯一次数、见证表 |
| POST | `/audit/confirm` | 凭原参数/版号/原截止日确认，条件封存或直返次数 |
| GET | `/table?version=&source=` | 取日表，缺省取现行；`source` 区分核准/估算 |
| GET | `/cumulative?through_day=&version=` | 月初到指定日累计量，标注是否已封存 |
| GET | `/versions` | 版本线、封存情况、现行标记 |
| GET | `/health` | 健康检查 |

### 请求示例

```bash
curl -X POST localhost:8000/convert -H 'Content-Type: application/json' -d '{
  "year": 2026, "month": 1,
  "sim_rain": [3,0,7,11,25,1,0,9,4,40,2,6,8,0,13,5,5,100,0,2,
               7,7,3,0,1,9,12,30,0,6],
  "approved": {"1": 5}
}'
curl -X POST localhost:8000/seal -H 'Content-Type: application/json' \
     -d '{"through_day": 10}'
curl -X POST localhost:8000/recompute -H 'Content-Type: application/json' \
     -d '{"approved": {"20": 15, "31": 8}}'
# 核查：连续 3 天累计 >= 50（十分之一毫米）的窗口数何时唯一
curl -X POST localhost:8000/audit/check -H 'Content-Type: application/json' \
     -d '{"n": 3, "threshold": 50}'
# 凭返回的 version / original_sealed_through 确认
curl -X POST localhost:8000/audit/confirm -H 'Content-Type: application/json' \
     -d '{"n": 3, "threshold": 50, "version": 2, "original_sealed_through": 10}'
```

## 5. 运行

```bash
docker compose up --build        # 服务在 http://localhost:8000，文档 /docs
# 数据持久化在命名卷 rain-data（/data/rain.db）
```

无 Docker 时本地运行：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
RAIN_DB_PATH=./data/rain.db uvicorn app.main:app --port 8000
```

## 6. 自测

```bash
pip install -r requirements.txt pytest httpx
RAIN_DB_PATH=/tmp/test.db python -m pytest tests/ -q   # 78 项
python3 -I tests/smoke_e2e.py                          # 原有端到端冒烟（需先起服务）
```

- `tests/test_alloc.py`：权重平衡、28/29/30/31 天月守恒、任意连续区间
  误差严格 <1（Fraction 精确断言）、核准值保留、各类无解、非法输入。
- `tests/test_audit.py`：候选枚举与**独立暴力枚举**（全部 floor/ceil
  组合逐一检验）40 组随机对照、钉死日约束、手工确定性小例的最少封存日
  与见证表、全月窗口天然唯一、全封存退化、参数校验。
- `tests/test_audit_api.py`：核查快照一致性、见证表守恒与区间误差、
  确认封存与幂等、补报后版号变化 409 退回、封存后截止日变化 409 退回、
  已足够时直接返回次数不新增封存。
- `tests/test_api.py`：转换/封存/补报全流程、无解保原表、取数过滤。
- 另做 200 组随机参数压力测试（随机精确份额 + 随机封存/核准 + 随机 N/T），
  候选集合与暴力实现逐一相等，最少封存日的最小性、唯一次数、见证表全部通过。

## 7. 实测结果（本环境）

- `pytest`：**78 passed**。
- 随机压力：**200/200** 通过（独立暴力枚举交叉验证）。
- 端到端冒烟（uvicorn + 真实 SQLite）：
  - 原冒烟（转换→封存→补报→无解保原表→累计查询）：**SMOKE OK**；
  - 核查冒烟：封存 2 天后 `(N=1,T=3)` 核查出现两个候选、次数 24 vs 25，
    建议封存到第 23 日并附见证表；补报后拿旧版号确认 → 409 且未新增封存；
    重新核查确认 → 条件封存成功（建议日 3，唯一次数 25）；截止日变化后
    旧参数确认 → 409；当前快照再确认 → `extended=false` 直返次数。
- `/docs` 与 `/openapi.json` 返回 200，新端点已纳入 OpenAPI。

## 8. 未完成项 / 边界说明

- 本沙箱无 Docker 守护进程，`docker compose build/up` 未实机执行；
  Dockerfile 与 compose 文件已通过 YAML/配置静态校验，容器内启动命令与
  本地实测的 uvicorn 命令一致，需在有 Docker 的环境补一次冒烟。
- 服务定位为**单站单月版本线**：对已存在不同年月的库再次 `/convert`
  返回 409，避免跨月覆盖；多站/多月并存需引入站点与月份维度（当前数据模型未含）。
- 封存只支持“月初到指定日”的前缀式封存（与需求一致）；未做鉴权/限流，
  内网或网关后部署为宜。
- 核查为只读计算，不产生版本；确认接口的条件封存复用既有 `seals` 表，
  不引入新表结构，旧数据库可直接升级。
