# 投研假设协作服务

面向证券研究中「公司 × 指标 × 预测期间 × 情景」假设管理的 Python 后端。仅依赖标准库，
解决多分析师协作时的五类问题：

1. **假设可追溯**：任一估值结果都能追到输入数据来源（含修订版本）、引用材料、常量与计算路径。
2. **外部修订不回写已发布观点**：数据源修订只把相关手工假设标为「过期(stale)」并沿公式血缘向下游传播，
   已生成的提交与发布内容保持不变。
3. **分支与精确合并**：分析师从已发布基线拉分支，提交采用乐观并发控制；冲突精确到
   `指标 × 期间 × 情景`（常量单独列出），返回三方取值（基线值 / 当前值 / 提交值）。
4. **公式防环、结论待重算可见**：公式版本化，依赖图加入前做环检测；输入缺失时结论进入
   `pending`，公式更新而旧提交未重算时进入 `outdated_formulas`。
5. **复核追加、发布唯一**：复核意见只可追加；状态机 `submitted → approved / changes_requested`；
   并发发布在进程内（线程锁）与跨进程（文件锁）下最多产生一个权威版本。导出包固定
   数据摘要（含 digest）、公式版本与审批人。

## 领域模型

| 实体 | 说明 |
| --- | --- |
| User | `analyst` / `reviewer` / `admin`；`watchlist_access` 控制敏感观察名单 |
| Company / Project | 项目隶属公司，声明 `periods`、`scenarios`、成员；`watchlist=true` 与普通项目权限隔离 |
| Metric | 项目内稳定 `key`（如 `revenue`、`fx_rate`） |
| DataSource | 外部数据来源，修订版本只增不减 |
| Reference | 引用材料（研报、年报等），可挂在具体假设取值上 |
| FormulaVersion | 某指标公式的不可变版本；表达式只允许白名单 AST |
| Branch / Commit | `baseline` 系统分支 + 工作分支；提交保存值快照、所用公式版本、常量与内容 digest |
| Assumption | 手工输入的索引记录，承载数据源绑定与 stale 标记 |
| Review / Comment | 评审单与只追加意见 |
| Release | 权威发布（每个项目至多一个），导出包不可变 |

取值分两类：`manual`（分析师录入，可绑数据源/引用/业务日期）与 `formula`（按依赖拓扑重算，
记录逐输入血缘）。业务时间（`business_date`）、系统时间（`set_at`/`created_at`）与版本分别保存。

### 公式

白名单算术表达式，示例：

```
gross_profit = revenue - cost
target       = gross_profit * fx_rate * shares * (1 - const("tax"))
qoq          = revenue - prev(revenue)
bull_spread  = scenario("bull", revenue) - revenue
anchor       = revenue - at(revenue, "2026Q1")
```

支持 `+ - * / **`、比较与 `if/else`、`min/max/abs/round`，以及 `prev / at / scenario / const`
四种特殊形式。禁止属性访问、导入、lambda、推导等任何非白名单语法。跨期间/跨情景引用不进入
同期间依赖图，因此 `prev(revenue)` 不会被误判为自环。

## 运行

需要 Python 3.11+：

```bash
python3 src/index.py            # 默认 0.0.0.0:8000，数据写入 .runtime/db.json
SERVERDB_SECRET=$(openssl rand -hex 32) SERVERDB_DB=/data/db.json python3 src/index.py
```

- `GET /health` 健康检查。
- 持久化：单 JSON 快照（临时文件 + 原子替换 + fsync）、`.audit.log` 只追加审计日志、
  旁路 `.lock` 文件锁；支持多进程/多线程并发。
- 首次启动后调用 `POST /api/v1/users` 创建第一个用户即成为 admin（bootstrap）。

测试：

```bash
python3 -m unittest discover -s tests
```

容器：`docker compose up --build`（建议把 `/data` 挂载为卷）。

## HTTP API（前缀 `/api/v1`，除登录外均需 `Authorization: Bearer <token>`）

| 方法 路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /auth/login` | - | 登录换令牌 |
| `POST/GET /users`，`PATCH /users/{name}` | admin | 用户与观察名单授权 |
| `POST/GET /companies` | 成员 | 公司 |
| `POST/GET /projects` | admin 建 / 成员看 | watchlist 项目自动对无权限者隐藏 |
| `GET /projects/{pid}`，`POST .../periods` | 成员 | 项目详情、追加预测期间 |
| `GET/POST /projects/{pid}/members` | admin | 成员管理 |
| `GET/POST /projects/{pid}/metrics` | analyst/admin 写 | 指标目录 |
| `GET/POST /data-sources`，`POST /data-sources/{sid}/revisions` | 修订仅 admin | 登记外部修订，返回被标 stale 的假设 |
| `GET/POST /projects/{pid}/references` | 成员 | 引用材料 |
| `GET/POST /projects/{pid}/formulas` | analyst/admin 写 | 创建公式新版本（防环、校验引用） |
| `POST /projects/{pid}/baseline/import`，`GET .../baseline` | admin / 成员 | 发布基线 |
| `POST/GET /projects/{pid}/branches`，`GET .../branches/{bid}` | analyst/admin 建 | 从已发布基线（或指定提交）分叉 |
| `POST /projects/{pid}/branches/{bid}/commits` | analyst/admin | 提交假设变更；带 `expected_parent` 做乐观并发 |
| `GET /projects/{pid}/commits/{cid}` / `.../status` | 成员 | 快照 / stale·pending·outdated 状态 |
| `GET /projects/{pid}/branches/{bid}/status` | 成员 | 分支头状态 |
| `GET /projects/{pid}/commits/{cid}/results/{metric}` | 成员 | 期间×情景结果矩阵（带 stale/pending 标记） |
| `GET /projects/{pid}/commits/{cid}/trace?metric=&period=&scenario=` | 成员 | 递归血缘：结果 → 公式 → 手工输入 → 数据源/引用/常量 |
| `GET /projects/{pid}/compare?a={commit}&b={commit}` | 成员 | 两版本差异（假设格、公式版本、常量）与人类可读解释 |
| `POST/GET /projects/{pid}/reviews`，`GET .../reviews/{rid}` | analyst 发起 | 评审 |
| `POST .../reviews/{rid}/comments` | 成员 | 意见**只能追加**，终结后禁止再写 |
| `POST .../reviews/{rid}/decision` | reviewer/admin | `approve` / `request_changes` / `reopen` |
| `POST/GET /projects/{pid}/releases`，`GET .../releases/{rid}` | reviewer/admin | 发布；已有权威版本时返回 409 `release_exists` |
| `GET /projects/{pid}/releases/{rid}/export` | 成员 | 固定数据摘要、公式版本、审批人的不可变导出 |

### 典型提交流程

```jsonc
// POST /projects/{pid}/branches/{bid}/commits
{
  "message": "2026Q2 收入上修，汇率更新为 7.15",
  "expected_parent": "c_……",          // 读取分支时拿到的头提交
  "constants": {"tax": 0.25},
  "changes": [
    {"metric_key": "revenue", "period": "2026Q2", "scenario": "base",
     "value": 121.0, "business_date": "2026-09-18",
     "data_source_id": "src_…", "reference_ids": ["ref_…"]},
    {"metric_key": "fx_rate", "period": "2026Q2", "scenario": "base",
     "value": 7.15, "data_source_id": "src_…"}
  ]
}
```

冲突响应（409 `merge_conflict`）：

```json
{
  "error": "merge_conflict",
  "expected_parent": "c_old", "current_head": "c_new",
  "assumption_conflicts": [
    {"metric_key": "revenue", "period": "2026Q2", "scenario": "base",
     "base_value": 110.0, "current_value": 115.0, "submitted_value": 130.0}
  ],
  "constant_conflicts": []
}
```

晨会追查路径：`results → trace（输入来源/计算路径）→ compare（两个版本为何不同）
→ status（哪些结论 stale/pending/公式过期仍待重算）→ release/export（固定摘要与审批人）`。
