# 投研假设协作服务

面向证券研究假设、情景与估值来源管理的 Python 后端服务。管理公司、指标、
数据来源、预测期间、情景、公式依赖、引用材料与审批版本，支持分析师从
已发布基线拉分支修改、比较与合并，并让晨会参与者从任一估值结果追到
输入来源与计算路径。

仅依赖 Python 标准库（3.11+），持久化写入 `.runtime/state.json`。

## 运行

```bash
python3 src/index.py          # 默认监听 8000 端口
python3 -m unittest discover -s tests
docker compose up --build
```

环境变量：`HOST` / `PORT` / `STATE_PATH` / `REFERENCE_PATH`。

## 核心规则

- **鉴权**：除 `GET /health` 外均需 `X-User-Id` 请求头。项目成员角色：
  `lead` / `analyst`（可写）、`reviewer` / `lead`（可审批发布）。
- **权限隔离**：`watchlist`（敏感观察名单）项目对非成员按不存在处理（404），
  不出现在其项目列表中；`normal` 项目全员可读、成员可写。
- **外部数据修订**：同（来源, 指标, 期间）的新 `observation` 视为修订，
  旧记录标记 `superseded_by`；相关假设仅被标记 `stale`，已发布版本快照不变。
- **公式防环**：`PUT /branches/{id}/formulas` 在写入前做依赖图判环，
  成环返回 `409 cycle`。表达式仅允许四则运算与 `min/max/abs/round`。
- **合并**：分支三方合并进 main，冲突精确到 指标×期间×情景，
  返回 `409 merge_conflict` 及 base/main/branch 三方取值。
- **发布**：`POST /projects/{id}/publish` 携带 `expected_version_id`，
  在锁内 compare-and-swap，基线过期返回 `409 stale_base`，
  并发发布最多产生一个权威版本（`authoritative: true`）。
- **复核意见**：只追加，无修改/删除接口。
- **导出**：`POST /versions/{id}/exports` 生成不可变导出，固定
  `data_digest`（数据快照摘要）、`formula_versions` 与 `approver_id`。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/users` | 创建用户 |
| GET/POST | `/projects` | 项目列表（按权限过滤）/ 创建 |
| GET | `/projects/{id}` | 项目详情 |
| GET/POST | `/projects/{id}/branches` | 分支列表 / 从已发布基线派生 |
| POST | `/projects/{id}/publish` | 发布新版本（CAS） |
| GET | `/projects/{id}/versions` | 版本列表 |
| GET/POST | `/projects/{id}/citations` | 引用材料 |
| GET/POST | `/companies`、`/periods`、`/sources` | 参考数据 |
| GET/POST | `/metrics` | 指标（可按 `?company_id=` 过滤） |
| POST | `/observations` | 登记外部数据 / 修订（触发过期标记） |
| GET/PUT | `/branches/{id}/assumptions` | 假设列表 /  upsert（指标×期间×情景） |
| GET/PUT | `/branches/{id}/formulas` | 公式列表 /  upsert（判环） |
| GET | `/branches/{id}/pending` | 待重算结论清单 |
| POST | `/branches/{id}/compute` | 估值计算，返回完整追溯树 |
| POST | `/branches/{id}/merge` | 三方合并进 main |
| GET | `/versions/{id}` | 版本详情（含快照） |
| GET | `/versions/{id}/diff?other={vid}` | 版本差异与数据/模型归因 |
| GET/POST | `/versions/{id}/comments` | 复核意见（只追加） |
| POST | `/versions/{id}/exports` | 生成导出 |
| GET | `/exports/{id}` | 读取导出 |

## 追溯与差异

`compute` 返回嵌套追溯树：每个节点标明经由假设（含来源、来源修订号、
引用材料）还是公式（含表达式与公式版本），并沿路径传播 `stale` 标记。
`diff` 比较两个版本的输入单元格与公式，把结论差异归因到 `data`（输入
假设变化）或 `model`（公式变化），复核人可据此判断差异来源。
