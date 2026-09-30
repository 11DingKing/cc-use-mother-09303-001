# 国际职教合作项目台账

本项目维护国际职教合作项目台账的领域约定、角色边界与样例数据，并交付一套**零外部依赖**（Python 3.11+ 标准库 + SQLite）的完整台账服务端，把**协议版本、参与机构、责任矩阵、里程碑、经费约束和验收证据**连成一本台账。

## 要解决的问题

签约三个月后，合作各方对「谁负责设备、师资和阶段报告」出现不同理解，原负责人离职又让若干承诺无人接手。本服务端用四条机制保证账实相符：

1. **签署时封存各方承诺**：签署（seal）对协议全部原约定生成快照哈希；封存后承诺事项、负责方、约定日期、预算金额、里程碑原约定由数据库触发器**物理禁写**，任何 UPDATE/DELETE 立即 ABORT。
2. **变更只能追加，不能改写**：责任移交、延期、部分验收、终止、经费拨付一律向哈希链 `amendments` 追加条目（`prev_hash`/`entry_hash` 链，首条接 `GENESIS`）。原约定永不修改，当前状态由「原始承诺 + 变更链」派生（如 `owner_org_id` 不变、`current_owner` 随移交变化；`due_date` 不变、`current_due_date` 随延期变化）。
3. **机构按权限看到自己的义务和依赖**：Bearer 令牌鉴权；秘书处可见全量责任矩阵、里程碑、变更链与封存校验；院校仅可见自己负责（签署负责方或移交后的当前负责方）的承诺，以及这些承诺 `depends_on` 链上的前置承诺；对无关方返回 404，不暴露存在性。
4. **批量更新中断后可继续、绝不重复改变状态**：每个物品独立事务，任务/资金变更与 `done` 标记同事务提交；崩溃时该物品整体回滚为 `pending`，已提交物品保持 `done`。`resume` 只处理 `pending`/可重试的 `failed`，永久跳过 `done`；单条写操作与整个批量任务都支持幂等键，重放返回首次结果，任务与资金状态不会被重复改变。

此外：**经费约束**——累计拨付不得超过按验收比例对应的预算金额（`disbursed ≤ budget × accepted/total`），并有预算总额 CHECK 双保险；**验收必须附证据**（报告/证书编号或链接），证据一经提交不可修改或删除。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/ledger_server/`：台账服务端
  - `database.py`：SQLite 表结构与封存/只追加触发器；
  - `ledger.py`：领域服务（签署封存、追加变更链、权限视图、经费约束、幂等批量）；
  - `httpapp.py`：标准库 HTTP/JSON 接口（每请求独立 SQLite 连接，可多线程）；
  - `__main__.py`：服务启动入口。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归、领域不变量（31 个用例）与 HTTP 端到端测试。

## 运行

```bash
# 启动服务（默认 127.0.0.1:8080，可用环境变量覆盖）
LEDGER_DB=ledger.db LEDGER_HOST=127.0.0.1 LEDGER_PORT=8080 \
  PYTHONPATH=src python3 -m ledger_server
# 或安装后：ledger-server
```

启动时自动建表并写入项目秘书处（固定 ID `org-secretariat`，默认令牌 `sek-secretariat-default-token`，生产环境请更换）。

### 典型流程

```bash
B=http://127.0.0.1:8080
S="Authorization: Bearer sek-secretariat-default-token"
J="-H Content-Type:application/json"

# 1) 秘书处登记院校，拿回各自令牌
curl -s -H "$S" $J -d '{"name":"中方职业学院","role":"中方院校"}' $B/api/orgs
curl -s -H "$S" $J -d '{"name":"莱茵技术大学","role":"外方院校"}' $B/api/orgs

# 2) 起草协议 → 加参与方 → 加责任矩阵承诺（含预算、依赖）→ 加里程碑 → 签署
curl -s -H "$S" $J -d '{"title":"国际职教论坛合作协议"}' $B/api/agreements
#   POST /api/agreements/{id}/parties | /commitments | /milestones
curl -s -H "$S" $J -d '{}' $B/api/agreements/{id}/seal     # 封存，返回 snapshot_hash

# 3) 负责人离职 → 责任移交（只追加，不改原约定）
curl -s -H "$S" $J -d '{"new_owner_org_id":"org-...","reason":"原负责人离职",
  "idempotency_key":"t-1"}' $B/api/commitments/{cid}/transfers

# 4) 机构查自己的义务与依赖
curl -s -H "Authorization: Bearer <院校令牌>" $B/api/agreements/{id}/obligations

# 5) 部分验收（附证据）→ 按验收比例拨付
curl -s -H "$S" $J -d '{"accepted_qty":12,"document_ref":"RPT-03",
  "idempotency_key":"a-1"}' $B/api/commitments/{cid}/acceptances
curl -s -H "$S" $J -d '{"amount":1200,"idempotency_key":"d-1"}' \
  $B/api/commitments/{cid}/disbursements

# 6) 可恢复批量更新（进度/验收/拨付混合），中断后 resume
curl -s -H "$S" $J -d '{"idempotency_key":"batch-1","items":[...]}' \
  $B/api/agreements/{id}/batches
curl -s -H "$S" $J -d '{}' $B/api/batches/{job_id}/resume
```

## HTTP 接口

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/health` | 公开 | 健康检查 |
| POST/GET | `/api/orgs` | 秘书处 | 登记机构 / 机构名录 |
| GET | `/api/me` | 任意机构 | 本方身份 |
| POST/GET | `/api/agreements` | 秘书处/参与方 | 起草 / 按权限列出协议 |
| GET | `/api/agreements/{id}` | 参与方 | 协议详情（秘书处含变更链、里程碑、封存校验） |
| POST | `/api/agreements/{id}/parties` | 秘书处 | 草稿期加入参与方 |
| POST | `/api/agreements/{id}/commitments` | 秘书处 | 草稿期追加责任矩阵承诺（含预算、依赖、数量） |
| POST | `/api/agreements/{id}/milestones` | 秘书处 | 草稿期追加里程碑 |
| POST | `/api/agreements/{id}/seal` | 秘书处 | 签署封存，返回快照哈希 |
| GET | `/api/agreements/{id}/obligations` | 参与方 | 本方义务与依赖视图 |
| POST | `/api/agreements/{id}/terminate` | 秘书处 | 终止（追加变更，未完成承诺派生为终止） |
| POST | `/api/commitments/{id}/transfers` | 秘书处 | 责任移交（追加 transfer） |
| POST | `/api/commitments/{id}/extensions` | 秘书处/当前负责方 | 延期（追加 extension，原始日期不变） |
| POST | `/api/commitments/{id}/acceptances` | 秘书处/当前负责方 | 部分验收，必须带 `document_ref` 证据 |
| POST | `/api/commitments/{id}/disbursements` | 秘书处 | 经费拨付（受验收比例与预算约束） |
| POST | `/api/commitments/{id}/progress` | 秘书处/当前负责方 | 标记执行中 |
| POST | `/api/milestones/{id}` | 秘书处 | 登记里程碑达成状态 |
| POST | `/api/agreements/{id}/batches` | 秘书处 | 创建可恢复批量任务（需 `idempotency_key`） |
| GET/POST | `/api/batches/{id}`、`/resume` | 秘书处/创建人 | 查询 / 中断后继续 |

错误统一为 `{"error": "...", "message": "..."}`，状态码：400 校验、403 越权、404 不存在/不可见、409 状态冲突。所有写操作建议带 `idempotency_key`。

## 数据模型要点

- `agreements`：`draft → sealed → terminated`；封存写入 `snapshot_hash`。
- `commitments`：责任矩阵行；`owner_org_id`（签署负责方，封存不变）与 `current_owner`（移交后派生）分离；`due_date`（原始，不变）与 `current_due_date`（延期派生）分离；`status/current_owner/accepted_qty` 为仅可随变更更新的派生列。
- `amendments`：`transfer / extension / partial_acceptance / termination / disbursement`，哈希链、只追加。
- `budget_lines`：预算金额不可变；`CHECK (disbursed <= amount)`。
- `acceptance_evidence`：验收证据，只追加、不可改删。
- `batch_jobs / batch_items`：批量任务（`running/completed/interrupted`）与每物品状态（`pending/done/failed`）。
- `idempotent_writes`：单条写操作幂等键，与业务变更在**同一事务**提交。

## 验证

```bash
# 全部自动化测试（领域不变量 + HTTP 端到端，共 31 个用例）
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 领域契约命令行检查
python3 tools/check_contract.py domain/contract.json
```

测试覆盖：封存快照哈希与触发器物理拒写、变更链顺序/哈希/只追加、责任移交与延期的原约定不变性、部分验收累计/超额拒绝/证据约束、经费按验收比例约束与幂等、终止后冻结、机构权限裁剪与存在性隐藏、批量任务全成功/单项失败续跑/提交前崩溃回滚恢复/幂等重放、跨进程持久化。
