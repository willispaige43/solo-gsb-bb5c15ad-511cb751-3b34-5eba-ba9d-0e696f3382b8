# 发票税额计算 API（Invoice Tax Calculator）

基于 **FastAPI + SQLite + Decimal** 的发票税额计算服务，支持 Docker Compose 一键启动。

- **全程 `Decimal` 高精度运算**：金额入参/出参一律为十进制字符串，绝不使用 `float`。
- **按币种最小单位舍入**：`ROUND_HALF_UP`（四舍五入）。USD/CNY 等按 0.01，JPY/KRW 按 1，KWD/BHD 按 0.001。
- **残差固定分配且可复算**：发票总额与「逐行舍入后税额之和」的差（残差）按**最大余数法**确定性地分配回具体行，任何时候用同一请求复算结果完全一致（响应中带 `recompute_matched` 标志）。
- **跨币种换算**：按请求汇率换算，原币金额完整保留，目标币独立做最小单位舍入与残差分配。
- **收款登记**：按发票登记收款（发票币种字符串、必须大于 0），随时查询收款记录与未收余额；累计收款不得超过含税总额，校验与写入在同一 SQLite 事务中完成，并发不会超收。
- **来款分配**：传入币种、金额字符串与待结发票 ID 列表，把一笔来款按**到期日升序（未设置到期日排最后）、同日按 ID 升序**逐张抵扣；只处理列表内发票，不做跨币种换算。金额必须是币种最小单位的正整数倍；列表重复、发票不存在、币种不符或总未收不足时**整笔拒绝、不留下部分收款**。每张被抵扣的发票沿用同一条 `payments` 流水，单张收款查询与逾期查询均可见。
- **到期日与逾期查询**：创建发票时可选填 ISO 日期 `due_date`；`GET /api/v1/invoices/overdue` 按截止日（可选 `as_of`，缺省取 UTC 当前日期）分页返回**已到期且未收余额大于零**的发票，含币种与精确（Decimal）未收余额。
- **SQLite 持久化**（WAL 模式），提供创建、按 id 查询、分页列表、健康检查。
- 内置 OpenAPI 交互文档（Swagger UI / ReDoc）。

---

## 1. 快速开始（Docker Compose）

前置条件：已安装 Docker（含 Compose v2：`docker compose`）。

```bash
# 构建镜像并后台启动
docker compose up -d --build

# 查看状态 / 日志
docker compose ps
docker compose logs -f

# 停止
docker compose down

# 停止并同时删除数据卷（清空所有发票数据）
docker compose down -v
```

启动后服务监听 **`http://localhost:8000`**：

| 地址 | 说明 |
| --- | --- |
| <http://localhost:8000/health> | 健康检查 |
| <http://localhost:8000/docs> | Swagger UI（可在线发请求） |
| <http://localhost:8000/redoc> | ReDoc 文档 |
| <http://localhost:8000/openapi.json> | OpenAPI 3.1 Schema |

> 宿主机端口默认 `8000`，被占用时可用 `API_PORT=8080 docker compose up -d` 改到 8080。

### 快速冒烟

```bash
curl -s http://localhost:8000/health
# {"status":"ok","database":"up"}

curl -s -X POST http://localhost:8000/api/v1/invoices \
  -H 'Content-Type: application/json' \
  -d '{
    "currency": "USD",
    "due_date": "2026-09-30",
    "lines": [
      {"description": "笔记本", "net": "10.05", "tax_rate": "0.10"},
      {"description": "签字笔", "net": "20.05", "tax_rate": "0.10"}
    ]
  }'

# 登记一笔收款并查看未收余额（假设上一步返回的发票 id 为 1）
curl -s -X POST http://localhost:8000/api/v1/invoices/1/payments \
  -H 'Content-Type: application/json' \
  -d '{"amount": "20.00"}'

curl -s http://localhost:8000/api/v1/invoices/1/payments

# 一笔来款分配到多张待结发票（按到期日升序、同日 ID 升序、无到期日排最后）
curl -s -X POST http://localhost:8000/api/v1/payment-allocations \
  -H 'Content-Type: application/json' \
  -d '{"currency": "USD", "amount": "50.00", "invoice_ids": [1, 2, 3]}'

# 查询已逾期且仍有未收余额的发票（as_of 缺省取 UTC 当前日期）
curl -s 'http://localhost:8000/api/v1/invoices/overdue?page=1&page_size=20&as_of=2026-10-15'
```

> 以上命令要求服务已启动：在仓库根目录执行 `docker compose up -d --build`（见下一节）。

该请求每行精确税额为 1.005 / 2.005（逐行 HALF_UP 得 1.01 / 2.01），但总税额
3.01（30.10×10%），行和 3.02，产生 **-0.01 残差**，按最大余数法从零头最小的
第 1 行扣减 1 个最小单位：

```json
{
  "id": 1,
  "rule_id": "half_up_plus_largest_remainder_v1",
  "currency": "USD",
  "currency_digits": 2,
  "lines": [
    {"line_no": 1, "description": "笔记本", "tax_rate": "0.10", "currency": "USD",
     "net": "10.05", "exact_tax": "1.005", "tax": "1.00", "gross": "11.05"},
    {"line_no": 2, "description": "签字笔", "tax_rate": "0.10", "currency": "USD",
     "net": "20.05", "exact_tax": "2.005", "tax": "2.01", "gross": "22.06"}
  ],
  "totals": {"currency": "USD", "total_net": "30.10", "total_tax": "3.01", "total_gross": "33.11"},
  "rounding": {
    "mode": "ROUND_HALF_UP",
    "minor_unit": "0.01",
    "residual_units": -1,
    "residual_amount": "-0.01",
    "method": "largest_remainder",
    "allocations": [{"line_no": 1, "delta_units": -1, "remainder": "0.005", "rank": 1}]
  },
  "due_date": "2026-09-30",
  "created_at": "2026-09-23T01:40:00Z",
  "recompute_matched": true
}
```

不传 `due_date` 时该字段返回 `null`，行为与旧版一致。

注意不变量：**逐行 `tax` 之和恒等于 `totals.total_tax`**（1.00 + 2.01 = 3.01）。

---

## 2. 计算规则（固定，可复算）

规则版本号 `rule_id = half_up_plus_largest_remainder_v1`：

1. 每行 **精确税额 = net × tax_rate**，Decimal 精度 50 位，全程无 float。
2. 每行税额按**币种最小单位**做 `ROUND_HALF_UP`。
3. 发票总税额 = Σ 各行精确税额后再做一次 `ROUND_HALF_UP`；它与逐行舍入税额之和
   的差称为**残差**（单位为最小单位的整数倍，`|残差| ≤ 行数`）。
4. 残差按 **最大余数法（largest remainder / Hare quota）** 分配：
   - **正残差**（总额更大，需补分）：按「精确税额相对其最小单位向下取整的零头」
     **从大到小**选行，每行 **+1 个最小单位**，分完为止；
   - **负残差**（需扣减）：按零头**从小到大**选行，每行 **-1 个最小单位**；
   - **零头相同按行号（请求中的先后顺序）决胜负**，因此结果完全确定、可复算。
   - 分配明细（哪一行、±几个最小单位、零头、名次）在响应的
     `rounding.allocations` 中可见、可审计。
5. **跨币种**：`target_currency` 与 `currency` 不同时必须提供 `exchange_rate`，
   标价方向为 `1 发票币种 = exchange_rate 目标币种`。原币行金额与总额原样保留
   （`lines[].net/tax/gross`、`totals`）；换算结果放在每行的 `converted` 与顶层
   `fx` 段，目标币独立执行一次相同的舍入 + 残差分配，同样有
   `fx.allocations` 审计明细。
6. 每次 `GET` 单张发票时，服务会用库里保存的原始请求重新计算并与存储结果比对，
   `recompute_matched=true` 即表示结果可被同一规则完整复现。

负数净额（冲红/贷记凭证）同样支持，规则不变。

### 支持的币种小数位

- 0 位：JPY、KRW、VND、XAF、XOF、ISK、CLP 等；
- 2 位：USD、EUR、CNY、GBP、HKD、AUD、CAD、SGD 等 ISO 4217 常见币种；
- 3 位：KWD、BHD、JOD、OMR、TND 等；
- 4 位：CLF、UYW。

不在内置 ISO 4217 表内的币种会返回 `422`；可用环境变量 `CURRENCY_DIGITS` 扩展
（见下文配置）。

---

## 3. API 一览

Base URL：`http://localhost:8000`（本地先用 Docker Compose 启动：
在仓库根目录执行 `docker compose up -d --build`，停止用 `docker compose down`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查（含数据库探活） |
| `POST` | `/api/v1/invoices` | 创建并计算发票（可选 `due_date`，持久化后返回完整结果），`201` |
| `GET` | `/api/v1/invoices/{id}` | 按 id 查询单张发票（含逐行明细与复算标志） |
| `GET` | `/api/v1/invoices?page=1&page_size=20` | 分页列表（按 id 倒序），`page_size` 范围 1~100 |
| `GET` | `/api/v1/invoices/overdue` | 逾期发票分页查询（按 id 倒序），支持 `page`、`page_size`、可选 `as_of` |
| `POST` | `/api/v1/invoices/{id}/payments` | 登记一笔收款（发票币种字符串、必须大于 0），`201` |
| `GET` | `/api/v1/invoices/{id}/payments` | 查询该发票的收款记录与未收余额 |
| `POST` | `/api/v1/payment-allocations` | 一笔来款在多张待结发票间按到期日顺序分配，`201` |
| `GET` | `/docs` `/redoc` `/openapi.json` | 交互式 API 文档 |

### 3.1 创建发票 `POST /api/v1/invoices`

请求体（**所有金额/税率/汇率都用字符串**）：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `currency` | string | 是 | 发票币种 ISO 4217 代码，如 `USD` |
| `lines[].net` | string | 是 | 不含税净额，必须是该币种最小单位的整数倍（如 USD 下 `100.00`，JPY 下 `100`）；不得为 `0`，负数表示冲红 |
| `lines[].tax_rate` | string | 是 | 税率，`0`~`1` 小数，`0.13` 表示 13% |
| `lines[].description` | string | 否 | 行备注，最长 500 字 |
| `target_currency` | string | 否 | 换算目标币种；与 `currency` 不同时必填 `exchange_rate` |
| `exchange_rate` | string | 条件必填 | 汇率：`1 currency = exchange_rate target_currency`，必须为正数 |
| `due_date` | string | 否 | 到期日，ISO 8601 **日期** `YYYY-MM-DD`（如 `2026-10-01`）；仅接受日期形状（带时间如 `2026-10-01T00:00:00Z`、不存在的日期均返回 `422`），缺省为 `null` 且不纳入逾期统计 |

跨币种示例：

```bash
curl -s -X POST http://localhost:8000/api/v1/invoices \
  -H 'Content-Type: application/json' \
  -d '{
    "currency": "USD",
    "target_currency": "CNY",
    "exchange_rate": "7.123456",
    "lines": [
      {"description": "咨询服务", "net": "100.00", "tax_rate": "0.13"},
      {"description": "差旅",     "net": "50.00",  "tax_rate": "0.06"}
    ]
  }'
```

要点：原币 `totals.total_tax = 16.00`；换算到 CNY 后逐行税额 92.60/21.37，
精确合计 113.975296 → 113.98，产生 +0.01 残差，分给零头更大的第 1 行
（`fx.allocations`），最终 CNY 税额 92.61 + 21.37 = **113.98**。

### 3.2 查询与列表

```bash
# 单张
curl -s http://localhost:8000/api/v1/invoices/1

# 分页列表
curl -s 'http://localhost:8000/api/v1/invoices?page=1&page_size=20'
```

分页响应：

```json
{
  "items": [
    {"id": 2, "currency": "USD", "target_currency": "CNY",
     "total_net": "150.00", "total_tax": "16.00", "total_gross": "166.00",
     "line_count": 2, "created_at": "2026-09-23T01:41:00Z"}
  ],
  "page": 1, "page_size": 20, "total": 2, "pages": 1
}
```

### 3.3 收款登记与未收余额

**登记收款** `POST /api/v1/invoices/{id}/payments`：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `amount` | string | 是 | 收款金额，**发票币种**的十进制字符串；必须 **大于 0**，且为币种最小单位的整数倍（USD 下 `20.00`，JPY 下 `2000`） |

规则：

- 每张发票的**累计收款不得超过含税总额** `total_gross`；超额请求返回 `422`，
  事务回滚，**不留下任何收款记录**。
- 余额校验与收款写入在**同一个 SQLite 事务**中完成（`BEGIN IMMEDIATE` 串行化
  并发写入），并发登记不会超收。
- 每笔收款保存登记时间 `created_at`（UTC，ISO 8601）。

```bash
# 登记 20.00 USD（发票 total_gross = 33.11）
curl -s -X POST http://localhost:8000/api/v1/invoices/1/payments \
  -H 'Content-Type: application/json' \
  -d '{"amount": "20.00"}'
```

响应 `201`（收款记录 + 最新余额）：

```json
{
  "id": 1,
  "invoice_id": 1,
  "amount": "20.00",
  "currency": "USD",
  "created_at": "2026-09-23T02:10:00Z",
  "paid_total": "20.00",
  "outstanding": "13.11"
}
```

**查询收款记录与未收余额** `GET /api/v1/invoices/{id}/payments`：

```bash
curl -s http://localhost:8000/api/v1/invoices/1/payments
```

```json
{
  "invoice_id": 1,
  "currency": "USD",
  "total_gross": "33.11",
  "paid_total": "20.00",
  "outstanding": "13.11",
  "payment_count": 1,
  "payments": [
    {"id": 1, "invoice_id": 1, "amount": "20.00", "currency": "USD",
     "created_at": "2026-09-23T02:10:00Z"}
  ]
}
```

### 3.4 逾期发票查询 `GET /api/v1/invoices/overdue`

查询参数：

| 参数 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `page` | int | 否 | `1` | 页码，从 1 开始；`< 1` 返回 `422` |
| `page_size` | int | 否 | `20` | 每页条数，范围 1~100；越界返回 `422` |
| `as_of` | string | 否 | UTC 当前日期 | 截止日，ISO 8601 **日期** `YYYY-MM-DD`；格式不符或日期不存在（含带时间成分）返回 `422` |

**逾期口径**（三者同时满足）：

1. 发票设置了 `due_date`（**缺少到期日的不纳入**）；
2. `due_date` **严格早于** `as_of`（到期日当天不算逾期）；
3. **未收余额大于零**（`total_gross − 已收合计 > 0`），已收满的发票不纳入。

结果**按 id 倒序**分页；每条包含发票 `id`、`currency`、`due_date`、含税总额
`total_gross`、已收合计 `paid_total` 与精确未收余额 `outstanding`（均为发票币种的
十进制字符串，余额计算全程 `Decimal`，沿用收款登记的最小单位规则），响应体额外
回显实际使用的 `as_of`。

```bash
# 指定截止日
curl -s 'http://localhost:8000/api/v1/invoices/overdue?page=1&page_size=20&as_of=2026-10-15'

# 不传 as_of：以 UTC 当前日期为截止日
curl -s 'http://localhost:8000/api/v1/invoices/overdue'
```

```json
{
  "items": [
    {
      "id": 1,
      "currency": "USD",
      "due_date": "2026-09-30",
      "total_gross": "33.11",
      "paid_total": "20.00",
      "outstanding": "13.11",
      "created_at": "2026-09-23T01:40:00Z"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1,
  "pages": 1,
  "as_of": "2026-10-15"
}
```

无逾期发票时 `items` 为空、`total`/`pages` 为 `0`。

### 3.5 来款分配 `POST /api/v1/payment-allocations`

把一笔来款在**多张同币种待结发票**间按到期日顺序逐张抵扣。

请求体（金额用字符串）：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `currency` | string | 是 | 来款币种 ISO 4217 代码；只分配给同币种发票，**不做跨币种换算** |
| `amount` | string | 是 | 来款总金额，必须 **大于 0** 且为币种最小单位的正整数倍（USD `100.00`，JPY `1000`） |
| `invoice_ids` | int[] | 是 | 待结发票 ID 列表，**至少 1 个且不得重复**；只处理列表中的发票 |

**抵扣规则**：

1. 将列表内发票按 `due_date` **升序**排列——**未设置到期日的发票排最后**；
   同一到期日（或都未设置到期日）按发票 **ID 升序**。
2. 按该顺序逐张抵扣，每张分配 `min(剩余来款, 该发票未收余额)`；来款用尽即止，
   之后的发票分配 `0`（**不产生**收款流水）。
3. 列表内发票**未收余额合计小于来款金额**时，整笔请求返回 `422`，事务回滚，
   **不会留下任何部分收款**。

**校验（全部不通过则整笔拒绝，无任何写入）**：

- 列表中任一发票不存在 → `404`；
- 任一发票币种与来款不一致 → `422`；
- 金额 ≤ 0、不是币种最小单位整数倍、非十进制字符串 → `422`；
- 列表为空或包含重复 ID → `422`；
- 总未收不足 → `422`。

余额读取、校验与多条流水写入在**同一个 `BEGIN IMMEDIATE` 写事务**内完成，
与单张收款接口共用同一串行化机制：来款分配之间、来款分配与单张收款并发时
都不会超收。

```bash
# 假设发票 2（due 2026-09-05，gross 22.00）、1（due 2026-09-10，gross 11.00）、
# 4（due 2026-09-10，gross 44.00）、3（无到期日，gross 33.00）
curl -s -X POST http://localhost:8000/api/v1/payment-allocations \
  -H 'Content-Type: application/json' \
  -d '{"currency": "USD", "amount": "50.00", "invoice_ids": [3, 4, 2, 1]}'
```

即使入参顺序是 `[3, 4, 2, 1]`，响应仍按实际抵扣顺序返回 `2 → 1 → 4 → 3`，
`201`：

```json
{
  "currency": "USD",
  "amount": "50.00",
  "allocated_total": "50.00",
  "created_at": "2026-09-23T03:00:00Z",
  "allocations": [
    {"invoice_id": 2, "due_date": "2026-09-05", "allocated": "22.00",
     "outstanding": "0.00", "payment_id": 10},
    {"invoice_id": 1, "due_date": "2026-09-10", "allocated": "11.00",
     "outstanding": "0.00", "payment_id": 11},
    {"invoice_id": 4, "due_date": "2026-09-10", "allocated": "17.00",
     "outstanding": "27.00", "payment_id": 12},
    {"invoice_id": 3, "due_date": null, "allocated": "0.00",
     "outstanding": "33.00", "payment_id": null}
  ]
}
```

- `allocated` 为该发票本次分配金额，`outstanding` 为抵扣后各自剩余未收余额；
- `payment_id` 指向沿用现有 `payments` 表写入的流水 ID；分配为 `0` 时为 `null`。
  因此 `GET /api/v1/invoices/{id}/payments` 与逾期分页查询都能看到分配结果，
  与单张收款登记完全一致。

### 3.6 错误码

| 状态码 | 场景 |
| --- | --- |
| `404` | 发票 id 不存在（含单张收款登记/查询、来款分配时列表中任一 id 不存在） |
| `422` | 参数错误：金额非十进制字符串、净额不满足最小单位、税率越界、跨币种缺汇率、未知币种、收款/来款金额 ≤ 0、金额小于币种最小单位、累计收款或列表总未收不足、来款列表为空或 ID 重复、来款与发票币种不一致、`due_date` / `as_of` 不是合法 ISO 日期、分页参数越界等（响应体 `detail` 为中文说明） |

---

## 4. 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `/data/invoices.db` | SQLite 文件路径；容器内位于数据卷 `/data`（WAL 旁路文件 `invoices.db-wal` / `-shm` 同目录） |
| `CURRENCY_DIGITS` | 空 | 追加/覆盖币种小数位，逗号分隔，如 `KWD:3,ABC:0`，取值 0~4 |
| `API_PORT`（仅 Compose） | `8000` | 宿主机映射端口，容器内固定 8000 |

容器内服务监听 `0.0.0.0:8000`，SQLite 数据保存在命名卷 `invoice-data` 中，
`docker compose down` 不会删数据；`docker compose down -v` 才会删除。

---

## 5. 本地开发（不用 Docker）

要求 Python 3.11+。

```bash
python -m venv .venv
source .venv/bin/1            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 默认 DB_PATH=/data/invoices.db，本地运行请改到可写目录
export DB_PATH="$(pwd)/data/invoices.db"
mkdir -p data
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## 6. 运行测试

```bash
pip install -r requirements.txt

# 计算核心（舍入 / 残差分配 / 跨币种 / 确定性），仅依赖标准库
python -m unittest tests.test_calc -v

# API 端到端（自动在空闲端口起真实 uvicorn：健康检查、增查、分页、收款登记/余额、
# 超额 422 不留记录、并发不超收、due_date 持久化、逾期分页/422、
# 来款分配排序/整笔拒绝/分配与单张收款混合并发不超收、持久化）
python -m unittest tests.test_api -v
```

---

## 7. 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由：健康检查 / 创建 / 查询 / 分页 / 逾期查询 / 单张收款 / 来款分配
│   ├── calc.py        # Decimal 计算核心：HALF_UP + 最大余数法残差分配（纯函数）
│   ├── currency.py    # ISO 4217 币种最小单位表 + CURRENCY_DIGITS 扩展
│   ├── schemas.py     # Pydantic 入参/出参模型（金额一律字符串，含 due_date / 逾期 / 来款分配模型）
│   └── db.py          # SQLite 建表（含旧库迁移）/ 事务写入（发票 + 收款 + 来款分配）/ 查询（含逾期，WAL）
├── tests/
│   ├── test_calc.py   # 20 个计算规则单测
│   └── test_api.py    # 真实 HTTP 端到端测试（含收款/来款分配并发不超收、到期日与逾期查询）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

存储模型：`invoices`（发票头 + 汇总金额 + 可选到期日 `due_date` + 原始请求 JSON，
供复算）、`invoice_lines`（逐行最终税额/含税及换算列）与 `payments`（收款登记：
金额、币种、登记时间）三张表，均通过外键级联删除。来款分配不引入新表：每张被
抵扣的发票向 `payments` 写入一条同构流水（整笔失败则全部不写），因此单张收款
查询与逾期查询天然可见分配结果。升级到含 `due_date` 的版本时，
服务启动会自动对旧库执行 `ALTER TABLE ... ADD COLUMN due_date`，历史发票的
到期日为空、不会出现在逾期结果中。
