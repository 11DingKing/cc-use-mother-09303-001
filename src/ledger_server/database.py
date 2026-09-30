"""SQLite 存储层。

设计要点：
- 签署（seal）后，commitments / budget_lines / milestones 的原约定列由触发器
  物理封存，任何改写立即 ABORT，原约定不可修改；
- amendments 只允许 INSERT，更新与删除同样被触发器拒绝，保证变更链只追加；
- 哈希链（协议快照哈希、变更链 prev_hash/entry_hash）由应用层计算，
  存储层只负责持久化与封存约束。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 参与机构
CREATE TABLE IF NOT EXISTS organizations (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    role        TEXT NOT NULL CHECK (role IN ('中方院校','外方院校','项目秘书处')),
    contact     TEXT NOT NULL DEFAULT '',
    auth_token  TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 协议版本
CREATE TABLE IF NOT EXISTS agreements (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    version_no      INTEGER NOT NULL CHECK (version_no >= 1),
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','sealed','terminated')),
    snapshot_hash   TEXT,                       -- 封存时计算
    sealed_at       TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (id, version_no)
);

-- 协议参与方（哪些机构签署了哪个版本）
CREATE TABLE IF NOT EXISTS agreement_parties (
    agreement_id    TEXT NOT NULL REFERENCES agreements(id),
    org_id          TEXT NOT NULL REFERENCES organizations(id),
    signed_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (agreement_id, org_id)
);

-- 责任矩阵的承诺行：设备 / 师资 / 阶段报告等
CREATE TABLE IF NOT EXISTS commitments (
    id              TEXT PRIMARY KEY,
    agreement_id    TEXT NOT NULL REFERENCES agreements(id),
    seq             INTEGER NOT NULL,           -- 协议内序号，参与快照哈希
    item            TEXT NOT NULL,              -- 事项，如「实训设备」
    category        TEXT NOT NULL DEFAULT 'other'
                    CHECK (category IN ('equipment','staff','report','funding','other')),
    owner_org_id    TEXT NOT NULL REFERENCES organizations(id),   -- 签署时的负责方
    depends_on      TEXT REFERENCES commitments(id),              -- 依赖的前置承诺
    due_date        TEXT,                       -- 签署时约定的原始日期（封存不变）
    current_due_date TEXT,                      -- 派生：延期后的当前生效日期
    detail          TEXT NOT NULL DEFAULT '',
    -- 以下为派生状态：只允许通过追加变更由系统更新
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','in_progress','partial_accepted',
                                      'accepted','transferred','delayed','terminated')),
    current_owner   TEXT NOT NULL REFERENCES organizations(id),   -- 当前负责方（移交后变化）
    accepted_qty    REAL NOT NULL DEFAULT 0 CHECK (accepted_qty >= 0),
    total_qty       REAL NOT NULL DEFAULT 1 CHECK (total_qty > 0),
    UNIQUE (agreement_id, seq)
);

-- 里程碑
CREATE TABLE IF NOT EXISTS milestones (
    id              TEXT PRIMARY KEY,
    agreement_id    TEXT NOT NULL REFERENCES agreements(id),
    seq             INTEGER NOT NULL,
    name            TEXT NOT NULL,
    due_date        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','met','partial','missed')),
    UNIQUE (agreement_id, seq)
);

-- 经费约束：每个承诺行一条预算
CREATE TABLE IF NOT EXISTS budget_lines (
    id              TEXT PRIMARY KEY,
    commitment_id   TEXT NOT NULL UNIQUE REFERENCES commitments(id),
    amount          REAL NOT NULL CHECK (amount >= 0),
    disbursed       REAL NOT NULL DEFAULT 0 CHECK (disbursed >= 0),
    CHECK (disbursed <= amount)
);

-- 验收证据
CREATE TABLE IF NOT EXISTS acceptance_evidence (
    id              TEXT PRIMARY KEY,
    commitment_id   TEXT NOT NULL REFERENCES commitments(id),
    accepted_qty    REAL NOT NULL CHECK (accepted_qty > 0),
    document_ref    TEXT NOT NULL,             -- 报告/证书编号或 URI
    note            TEXT NOT NULL DEFAULT '',
    recorded_by     TEXT NOT NULL REFERENCES organizations(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 追加式变更链：责任移交 / 延期 / 部分验收 / 终止 / 拨付
CREATE TABLE IF NOT EXISTS amendments (
    id              TEXT PRIMARY KEY,
    agreement_id    TEXT NOT NULL REFERENCES agreements(id),
    seq             INTEGER NOT NULL,           -- 协议内单调递增
    kind            TEXT NOT NULL
                    CHECK (kind IN ('transfer','extension','partial_acceptance',
                                    'termination','disbursement')),
    commitment_id   TEXT REFERENCES commitments(id),
    payload_json    TEXT NOT NULL,              -- 变更内容（原样入链）
    requested_by    TEXT NOT NULL REFERENCES organizations(id),
    prev_hash       TEXT NOT NULL,              -- 前一条 entry_hash，首条为 GENESIS
    entry_hash      TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (agreement_id, seq)
);

-- 可恢复批量更新任务
CREATE TABLE IF NOT EXISTS batch_jobs (
    id              TEXT PRIMARY KEY,
    agreement_id    TEXT NOT NULL REFERENCES agreements(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running','completed','interrupted')),
    created_by      TEXT NOT NULL REFERENCES organizations(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT
);

CREATE TABLE IF NOT EXISTS batch_items (
    job_id          TEXT NOT NULL REFERENCES batch_jobs(id),
    item_index      INTEGER NOT NULL,
    op              TEXT NOT NULL
                    CHECK (op IN ('progress','partial_acceptance','disbursement')),
    commitment_id   TEXT NOT NULL REFERENCES commitments(id),
    payload_json    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','done','skipped','failed')),
    result_json     TEXT,
    error           TEXT,
    PRIMARY KEY (job_id, item_index)
);

-- 幂等键记录：单条写操作去重（重放返回首次结果）
CREATE TABLE IF NOT EXISTS idempotent_writes (
    key             TEXT PRIMARY KEY,
    result_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ===== 封存触发器：签署后承诺的原约定列不可改写 =====
-- 只拦截原约定列；current_owner / status / accepted_qty 为系统派生列，
-- 仅能在追加 amendment 的同一事务内更新。
CREATE TRIGGER IF NOT EXISTS trg_commitments_seal_update
BEFORE UPDATE ON commitments
WHEN (SELECT status FROM agreements WHERE id = NEW.agreement_id) IN ('sealed','terminated')
 AND (NEW.item IS NOT OLD.item
   OR NEW.category IS NOT OLD.category
   OR NEW.owner_org_id IS NOT OLD.owner_org_id
   OR IFNULL(NEW.depends_on,'') IS NOT IFNULL(OLD.depends_on,'')
   OR IFNULL(NEW.due_date,'') IS NOT IFNULL(OLD.due_date,'')
   OR NEW.detail IS NOT OLD.detail
   OR NEW.seq IS NOT OLD.seq
   OR NEW.total_qty IS NOT OLD.total_qty)
BEGIN
    SELECT RAISE(ABORT, 'commitment original terms are sealed and immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_commitments_seal_delete
BEFORE DELETE ON commitments
WHEN (SELECT status FROM agreements WHERE id = OLD.agreement_id) IN ('sealed','terminated')
BEGIN
    SELECT RAISE(ABORT, 'commitments of a sealed agreement cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS trg_budget_amount_immutable
BEFORE UPDATE OF amount ON budget_lines
BEGIN
    SELECT RAISE(ABORT, 'budget amount is immutable; disburse via disbursement amendment');
END;

CREATE TRIGGER IF NOT EXISTS trg_budget_no_delete
BEFORE DELETE ON budget_lines
BEGIN
    SELECT RAISE(ABORT, 'budget lines cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS trg_milestones_seal
BEFORE UPDATE ON milestones
WHEN (SELECT status FROM agreements WHERE id = OLD.agreement_id) IN ('sealed','terminated')
 AND (NEW.name IS NOT OLD.name OR NEW.due_date IS NOT OLD.due_date
      OR NEW.seq IS NOT OLD.seq)
BEGIN
    SELECT RAISE(ABORT, 'milestone original terms are sealed and immutable');
END;

-- ===== 变更链只追加 =====
CREATE TRIGGER IF NOT EXISTS trg_amendments_no_update
BEFORE UPDATE ON amendments
BEGIN
    SELECT RAISE(ABORT, 'amendment chain is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_amendments_no_delete
BEFORE DELETE ON amendments
BEGIN
    SELECT RAISE(ABORT, 'amendment chain is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_evidence_no_update
BEFORE UPDATE ON acceptance_evidence
BEGIN
    SELECT RAISE(ABORT, 'acceptance evidence is immutable once submitted');
END;

CREATE TRIGGER IF NOT EXISTS trg_evidence_no_delete
BEFORE DELETE ON acceptance_evidence
BEGIN
    SELECT RAISE(ABORT, 'acceptance evidence cannot be deleted');
END;
"""


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """打开（必要时初始化）数据库连接。"""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


SEED_SECRETARIAT_SQL = """
INSERT OR IGNORE INTO organizations (id, name, role, contact, auth_token)
VALUES ('org-secretariat', '项目秘书处', '项目秘书处',
        'secretariat@forum.example', 'sek-secretariat-default-token')
"""


def seed_secretariat(conn: sqlite3.Connection) -> str:
    """确保秘书处存在，返回其固定 ID。"""
    conn.execute(SEED_SECRETARIAT_SQL)
    conn.commit()
    return "org-secretariat"
