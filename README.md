# 气象数仓 · 模拟降雨转公历日表服务

单站单月的 30 个模拟日雨量（十分之一毫米，非负整数）按两种日历的重叠比例
拆成公历日表，支持核准日照录、版本封存、补报更正。FastAPI + SQLite，
Docker Compose 一键启动。

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

## 3. API 一览（启动后见 `/docs` 在线试用）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/convert` | 初始转换，生成版本并切为现行（201） |
| POST | `/seal` | 封存现行版本月初到指定日 |
| POST | `/recompute` | 补报/更正重算（限未封存日，保留封存值） |
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
```

## 4. 运行

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

## 5. 自测

```bash
pip install -r requirements.txt pytest httpx
RAIN_DB_PATH=/tmp/test.db python -m pytest tests/ -q   # 21 项
python3 -I tests/smoke_e2e.py                          # 端到端冒烟（需先起服务）
```

- `tests/test_alloc.py`：权重平衡、28/29/30/31 天月守恒、任意连续区间
  误差严格 <1（Fraction 精确断言）、核准值保留、各类无解、非法输入。
- `tests/test_api.py`：转换/封存/补报全流程、封存日拒绝更正、
  封存值与来源标签保留、模拟月更正后封存不变、无解保原表、取数过滤。
- 另做 400 组随机参数压力测试，全部通过守恒、非负、区间误差、核准保留检查。

## 6. 实测结果（本环境）

- `pytest`：**21 passed**。
- 随机压力：**400/400** 通过。
- 端到端冒烟（uvicorn + 真实 SQLite）：转换 v1（31 天，总量 316 守恒）→
  封存 1~10 日（累计 88）→ 更正封存日被 422 拒绝且现行版不变 →
  补报第 20/31 日生成 v2，封存前缀逐日数值与来源标签不变、总量仍 316 →
  无解请求（核准超量）返回 422，原表 v2 继续供应 → 累计查询封存标记正确。
- `/docs` 与 `/openapi.json` 返回 200；数据库四张表与封存继承记录核对无误。

## 7. 未完成项 / 边界说明

- 本沙箱无 Docker 守护进程，`docker compose build/up` 未实机执行；
  Dockerfile 与 compose 文件已通过 YAML/配置静态校验，容器内启动命令与
  本地实测的 uvicorn 命令一致，需在有 Docker 的环境补一次冒烟。
- 服务定位为**单站单月版本线**：对已存在不同年月的库再次 `/convert`
  返回 409，避免跨月覆盖；多站/多月并存需引入站点与月份维度（当前数据模型未含）。
- 封存只支持“月初到指定日”的前缀式封存（与需求一致）；未做鉴权/限流，
  内网或网关后部署为宜。
