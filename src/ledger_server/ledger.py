"""台账领域服务。

在 SQLite 存储之上实现四条核心不变量：

1. 签署封存：``seal`` 时对协议全部原约定生成快照哈希；封存后原约定列
   由数据库触发器物理拒写；
2. 追加式变更：责任移交 / 延期 / 部分验收 / 终止 / 拨付一律向 amendments
   追加哈希链条目，当前状态由「原约定 + 变更链」派生，原约定永不被改写；
3. 权限视图：秘书处可见全量；院校仅可见自己负责（签署时负责方或移交后的
   当前负责方）的承诺，以及这些承诺依赖的前置承诺；
4. 可恢复批量更新：每个物品独立事务、状态与资金变更在同一事务内落库并
   标记 done；中断后 resume 跳过 done、继续 pending，配合幂等键重放，
   任务与资金状态不会被重复改变。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

GENESIS = "GENESIS"


class LedgerError(Exception):
    """领域错误基类。"""


class ValidationError(LedgerError):
    """请求数据不合法。"""


class Forbidden(LedgerError):
    """机构无权执行该操作。"""


class NotFound(LedgerError):
    """对象不存在或对当前机构不可见。"""


class Conflict(LedgerError):
    """对象当前状态不允许该操作。"""


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ledger:
    """台账领域服务。所有方法均在调用方提供的连接上执行。"""

    SECRETARIAT = "org-secretariat"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ 工具

    def _row(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, list(params)).fetchone()

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, list(params)).fetchall())

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict | None:
        return dict(row) if row is not None else None

    def _get_org(self, org_id: str) -> sqlite3.Row:
        row = self._row("SELECT * FROM organizations WHERE id = ?", (org_id,))
        if row is None:
            raise NotFound(f"机构不存在：{org_id}")
        return row

    def _authenticate(self, token: str) -> sqlite3.Row:
        row = self._row("SELECT * FROM organizations WHERE auth_token = ?", (token,))
        if row is None:
            raise Forbidden("令牌无效")
        return row

    def _get_agreement(self, agreement_id: str) -> sqlite3.Row:
        row = self._row("SELECT * FROM agreements WHERE id = ?", (agreement_id,))
        if row is None:
            raise NotFound(f"协议不存在：{agreement_id}")
        return row

    def _require_sealed(self, agreement_id: str) -> sqlite3.Row:
        agreement = self._get_agreement(agreement_id)
        if agreement["status"] == "draft":
            raise Conflict("协议尚未签署封存，不能追加变更")
        return agreement

    def _require_active_agreement(self, agreement_id: str) -> sqlite3.Row:
        """已封存且未终止：只有活跃协议允许追加执行类变更。"""
        agreement = self._require_sealed(agreement_id)
        if agreement["status"] == "terminated":
            raise Conflict("协议已终止，不能再追加变更")
        return agreement

    def _is_secretariat(self, org_id: str) -> bool:
        org = self._get_org(org_id)
        return org["role"] == "项目秘书处"

    def _require_secretariat(self, actor: str) -> None:
        if not self._is_secretariat(actor):
            raise Forbidden("仅项目秘书处可执行该操作")

    # ------------------------------------------------------------ 机构管理

    def create_organization(self, actor_token: str, name: str, role: str,
                            contact: str = "") -> dict:
        """秘书处登记参与机构并颁发访问令牌。"""
        actor = self._authenticate(actor_token)
        self._require_secretariat(actor["id"])
        if role not in ("中方院校", "外方院校", "项目秘书处"):
            raise ValidationError("机构角色必须是中方院校/外方院校/项目秘书处")
        if not name:
            raise ValidationError("机构名称不能为空")
        org_id = _new_id("org")
        token = "org-" + uuid.uuid4().hex
        try:
            self.conn.execute(
                "INSERT INTO organizations (id, name, role, contact, auth_token)"
                " VALUES (?,?,?,?,?)",
                (org_id, name, role, contact, token),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"机构名称已存在：{name}") from exc
        return {"id": org_id, "name": name, "role": role, "auth_token": token}

    def list_organizations(self, actor_token: str) -> list[dict]:
        actor = self._authenticate(actor_token)
        self._require_secretariat(actor["id"])
        rows = self._all(
            "SELECT id, name, role, contact, created_at FROM organizations ORDER BY id")
        return [dict(r) for r in rows]

    def whoami(self, actor_token: str) -> dict:
        return dict(self._authenticate(actor_token))

    # ------------------------------------------------------------ 协议起草

    def create_agreement(self, actor_token: str, title: str,
                         version_no: int = 1) -> dict:
        actor = self._authenticate(actor_token)
        self._require_secretariat(actor["id"])
        if not title:
            raise ValidationError("协议标题不能为空")
        agreement_id = _new_id("agr")
        self.conn.execute(
            "INSERT INTO agreements (id, title, version_no, status) VALUES (?,?,?,'draft')",
            (agreement_id, title, int(version_no)),
        )
        self.conn.commit()
        return self.agreement_detail(actor_token, agreement_id)

    def add_party(self, actor_token: str, agreement_id: str, org_id: str) -> dict:
        actor = self._authenticate(actor_token)
        self._require_secretariat(actor["id"])
        agreement = self._get_agreement(agreement_id)
        if agreement["status"] != "draft":
            raise Conflict("协议已封存，参与方不可更改")
        self._get_org(org_id)
        try:
            self.conn.execute(
                "INSERT INTO agreement_parties (agreement_id, org_id) VALUES (?,?)",
                (agreement_id, org_id),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            pass  # 已在参与方名单中，幂等
        return {"agreement_id": agreement_id, "org_id": org_id}

    def add_commitment(self, actor_token: str, agreement_id: str, item: str,
                       owner_org_id: str, category: str = "other",
                       depends_on: str | None = None, due_date: str | None = None,
                       detail: str = "", total_qty: float = 1,
                       budget_amount: float = 0) -> dict:
        """在草稿协议中追加一条责任矩阵承诺行（含预算）。"""
        actor = self._authenticate(actor_token)
        self._require_secretariat(actor["id"])
        agreement = self._get_agreement(agreement_id)
        if agreement["status"] != "draft":
            raise Conflict("协议已封存，责任矩阵只能通过追加变更调整")
        if not item:
            raise ValidationError("承诺事项不能为空")
        self._get_org(owner_org_id)
        party = self._row(
            "SELECT 1 FROM agreement_parties WHERE agreement_id=? AND org_id=?",
            (agreement_id, owner_org_id))
        if party is None:
            raise ValidationError("承诺负责方必须是协议参与机构")
        if depends_on is not None:
            dep = self._row(
                "SELECT * FROM commitments WHERE id=? AND agreement_id=?",
                (depends_on, agreement_id))
            if dep is None:
                raise ValidationError("依赖的前置承诺不存在或不属于本协议")
        if total_qty <= 0:
            raise ValidationError("total_qty 必须为正数")
        if budget_amount < 0:
            raise ValidationError("预算金额不能为负")
        seq_row = self._row(
            "SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM commitments WHERE agreement_id=?",
            (agreement_id,))
        commitment_id = _new_id("com")
        self.conn.execute(
            "INSERT INTO commitments (id, agreement_id, seq, item, category,"
            " owner_org_id, current_owner, depends_on, due_date, current_due_date,"
            " detail, total_qty) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (commitment_id, agreement_id, seq_row["next_seq"], item, category,
             owner_org_id, owner_org_id, depends_on, due_date, due_date, detail,
             float(total_qty)),
        )
        self.conn.execute(
            "INSERT INTO budget_lines (id, commitment_id, amount, disbursed)"
            " VALUES (?,?,?,0)",
            (_new_id("bud"), commitment_id, float(budget_amount)),
        )
        self.conn.commit()
        return self._commitment_view(commitment_id)

    def add_milestone(self, actor_token: str, agreement_id: str, name: str,
                      due_date: str) -> dict:
        actor = self._authenticate(actor_token)
        self._require_secretariat(actor["id"])
        agreement = self._get_agreement(agreement_id)
        if agreement["status"] != "draft":
            raise Conflict("协议已封存，里程碑原约定不可更改")
        if not name or not due_date:
            raise ValidationError("里程碑名称与日期不能为空")
        seq_row = self._row(
            "SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM milestones WHERE agreement_id=?",
            (agreement_id,))
        milestone_id = _new_id("mil")
        self.conn.execute(
            "INSERT INTO milestones (id, agreement_id, seq, name, due_date, status)"
            " VALUES (?,?,?,?,?,'pending')",
            (milestone_id, agreement_id, seq_row["next_seq"], name, due_date),
        )
        self.conn.commit()
        row = self._row("SELECT * FROM milestones WHERE id=?", (milestone_id,))
        return dict(row)

    # ------------------------------------------------------------ 签署封存

    def _snapshot_payload(self, agreement_id: str) -> dict:
        commitments = []
        for r in self._all(
                "SELECT c.seq, c.item, c.category, c.owner_org_id, c.depends_on,"
                " c.due_date, c.detail, c.total_qty, b.amount AS budget_amount"
                " FROM commitments c JOIN budget_lines b ON b.commitment_id=c.id"
                " WHERE c.agreement_id=? ORDER BY c.seq", (agreement_id,)):
            commitments.append({k: r[k] for k in r.keys()})
        milestones = [
            {k: r[k] for k in r.keys()}
            for r in self._all(
                "SELECT seq, name, due_date FROM milestones"
                " WHERE agreement_id=? ORDER BY seq", (agreement_id,))
        ]
        parties = [
            r["org_id"] for r in self._all(
                "SELECT org_id FROM agreement_parties WHERE agreement_id=?"
                " ORDER BY org_id", (agreement_id,))
        ]
        agreement = self._get_agreement(agreement_id)
        return {
            "agreement_id": agreement_id,
            "title": agreement["title"],
            "version_no": agreement["version_no"],
            "parties": parties,
            "commitments": commitments,
            "milestones": milestones,
        }

    def seal_agreement(self, actor_token: str, agreement_id: str) -> dict:
        """签署：校验参与方与责任矩阵，封存快照并计算哈希。"""
        actor = self._authenticate(actor_token)
        self._require_secretariat(actor["id"])
        agreement = self._get_agreement(agreement_id)
        if agreement["status"] != "draft":
            raise Conflict("协议不是草稿状态，不能签署")
        parties = self._all(
            "SELECT org_id FROM agreement_parties WHERE agreement_id=?",
            (agreement_id,))
        if len(parties) < 2:
            raise ValidationError("签署至少需要两方参与机构")
        commitments = self._all(
            "SELECT * FROM commitments WHERE agreement_id=?", (agreement_id,))
        if not commitments:
            raise ValidationError("责任矩阵为空，不能签署")
        for c in commitments:
            if not c["due_date"]:
                raise ValidationError(f"承诺「{c['item']}」缺少约定完成日期")

        snapshot = self._snapshot_payload(agreement_id)
        snapshot_hash = _sha256(_canonical(snapshot))
        self.conn.execute(
            "UPDATE agreements SET status='sealed', snapshot_hash=?, sealed_at=?"
            " WHERE id=?",
            (snapshot_hash, _now(), agreement_id),
        )
        self.conn.commit()
        return {
            "agreement_id": agreement_id,
            "status": "sealed",
            "snapshot_hash": snapshot_hash,
            "snapshot": snapshot,
        }

    def verify_seal(self, agreement_id: str) -> dict:
        """重算快照哈希并校验变更链，供审计使用。"""
        agreement = self._get_agreement(agreement_id)
        snapshot = self._snapshot_payload(agreement_id)
        recomputed = _sha256(_canonical(snapshot))
        seal_ok = agreement["snapshot_hash"] == recomputed

        chain_ok = True
        previous = GENESIS
        for amd in self._all(
                "SELECT * FROM amendments WHERE agreement_id=? ORDER BY seq",
                (agreement_id,)):
            expected = _sha256(_canonical({
                "agreement_id": amd["agreement_id"],
                "seq": amd["seq"],
                "kind": amd["kind"],
                "commitment_id": amd["commitment_id"],
                "payload": json.loads(amd["payload_json"]),
                "requested_by": amd["requested_by"],
                "prev_hash": amd["prev_hash"],
            }))
            if amd["prev_hash"] != previous or amd["entry_hash"] != expected:
                chain_ok = False
                break
            previous = amd["entry_hash"]
        return {
            "agreement_id": agreement_id,
            "seal_intact": seal_ok,
            "chain_intact": chain_ok,
            "snapshot_hash": agreement["snapshot_hash"],
            "recomputed_hash": recomputed,
        }

    # ------------------------------------------------------------ 追加变更

    def _append_amendment(self, conn: sqlite3.Connection, agreement_id: str,
                          kind: str, payload: dict, actor: str,
                          commitment_id: str | None = None) -> sqlite3.Row:
        """在调用方事务内追加一条哈希链变更。"""
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM amendments"
            " WHERE agreement_id=?", (agreement_id,)).fetchone()
        seq = seq_row["next_seq"]
        prev_row = conn.execute(
            "SELECT entry_hash FROM amendments WHERE agreement_id=?"
            " ORDER BY seq DESC LIMIT 1", (agreement_id,)).fetchone()
        prev_hash = prev_row["entry_hash"] if prev_row else GENESIS
        amendment_id = _new_id("amd")
        entry_hash = _sha256(_canonical({
            "agreement_id": agreement_id,
            "seq": seq,
            "kind": kind,
            "commitment_id": commitment_id,
            "payload": payload,
            "requested_by": actor,
            "prev_hash": prev_hash,
        }))
        conn.execute(
            "INSERT INTO amendments (id, agreement_id, seq, kind, commitment_id,"
            " payload_json, requested_by, prev_hash, entry_hash)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (amendment_id, agreement_id, seq, kind, commitment_id,
             _canonical(payload), actor, prev_hash, entry_hash),
        )
        return conn.execute("SELECT * FROM amendments WHERE id=?",
                            (amendment_id,)).fetchone()

    def _idempotent(self, key: str | None, producer):
        """带幂等键的写操作。

        幂等键记录必须与业务变更在同一事务提交：否则进程在两者之间崩溃，
        重放会第二次改变任务或资金状态。
        """
        if key is None:
            with self.conn:
                return producer()
        existing = self._row(
            "SELECT result_json FROM idempotent_writes WHERE key=?", (key,))
        if existing is not None:
            return {"replayed": True, **json.loads(existing["result_json"])}
        try:
            with self.conn:
                dup = self.conn.execute(
                    "SELECT result_json FROM idempotent_writes WHERE key=?",
                    (key,)).fetchone()
                if dup is not None:  # 事务内复查，挡住并发同键请求
                    return {"replayed": True, **json.loads(dup["result_json"])}
                result = producer()
                self.conn.execute(
                    "INSERT INTO idempotent_writes (key, result_json) VALUES (?,?)",
                    (key, _canonical(result)),
                )
            return result
        except sqlite3.IntegrityError:
            row = self._row(
                "SELECT result_json FROM idempotent_writes WHERE key=?", (key,))
            if row is not None:
                return {"replayed": True, **json.loads(row["result_json"])}
            raise

    def _get_visible_commitment(self, commitment_id: str,
                                actor: sqlite3.Row) -> sqlite3.Row:
        row = self._row("SELECT * FROM commitments WHERE id=?", (commitment_id,))
        if row is None:
            raise NotFound(f"承诺不存在：{commitment_id}")
        if actor["role"] == "项目秘书处":
            return row
        visible = {r["id"] for r in
                   self._visible_commitments(actor["id"], row["agreement_id"])}
        if commitment_id not in visible:
            # 不暴露存在性
            raise NotFound(f"承诺不存在：{commitment_id}")
        return row

    def transfer_responsibility(self, actor_token: str, commitment_id: str,
                                new_owner_org_id: str, reason: str,
                                idempotency_key: str | None = None) -> dict:
        """责任移交：只追加 transfer 变更并更新派生当前负责方。"""
        actor = self._authenticate(actor_token)
        commitment = self._get_visible_commitment(commitment_id, actor)
        self._require_secretariat(actor["id"])
        agreement = self._require_sealed(commitment["agreement_id"])
        if agreement["status"] == "terminated" or commitment["status"] == "terminated":
            raise Conflict("协议或承诺已终止，不能移交责任")
        new_owner = self._get_org(new_owner_org_id)
        party = self._row(
            "SELECT 1 FROM agreement_parties WHERE agreement_id=? AND org_id=?",
            (commitment["agreement_id"], new_owner_org_id))
        if party is None:
            raise ValidationError("接收方必须是协议参与机构")
        if new_owner_org_id == commitment["current_owner"]:
            raise Conflict("接收方已是当前负责方")
        payload = {
            "commitment_id": commitment_id,
            "item": commitment["item"],
            "from_org_id": commitment["current_owner"],
            "to_org_id": new_owner_org_id,
            "reason": reason,
        }

        def produce() -> dict:
            # 事务由 _idempotent 统一管理：链条目、派生状态与幂等键同提交
            try:
                amd = self._append_amendment(
                    self.conn, commitment["agreement_id"], "transfer",
                    payload, actor["id"], commitment_id)
                self.conn.execute(
                    "UPDATE commitments SET current_owner=?, status='transferred'"
                    " WHERE id=?",
                    (new_owner_org_id, commitment_id))
            except sqlite3.IntegrityError as exc:
                raise Conflict(str(exc)) from exc
            return {"amendment_id": amd["id"], "seq": amd["seq"],
                    "entry_hash": amd["entry_hash"], "kind": "transfer",
                    "payload": payload}

        return self._idempotent(idempotency_key, produce)

    def extend_due_date(self, actor_token: str, commitment_id: str,
                        new_due_date: str, reason: str,
                        idempotency_key: str | None = None) -> dict:
        """延期：原始 due_date 不动，只追加 extension 并更新派生日期。"""
        actor = self._authenticate(actor_token)
        commitment = self._get_visible_commitment(commitment_id, actor)
        self._require_active_agreement(commitment["agreement_id"])
        if commitment["status"] == "accepted":
            raise Conflict("承诺已全额验收，不能延期")
        if actor["role"] != "项目秘书处" and actor["id"] != commitment["current_owner"]:
            raise Forbidden("仅秘书处或当前负责方可申请延期")
        if not new_due_date:
            raise ValidationError("新日期不能为空")
        if commitment["current_due_date"] and new_due_date <= commitment["current_due_date"]:
            raise ValidationError("延期后的日期必须晚于当前生效日期")
        payload = {
            "commitment_id": commitment_id,
            "item": commitment["item"],
            "original_due_date": commitment["due_date"],
            "from_due_date": commitment["current_due_date"],
            "to_due_date": new_due_date,
            "reason": reason,
        }

        def produce() -> dict:
            amd = self._append_amendment(
                self.conn, commitment["agreement_id"], "extension",
                payload, actor["id"], commitment_id)
            self.conn.execute(
                "UPDATE commitments SET current_due_date=?, status='delayed'"
                " WHERE id=?",
                (new_due_date, commitment_id))
            return {"amendment_id": amd["id"], "seq": amd["seq"],
                    "entry_hash": amd["entry_hash"], "kind": "extension",
                    "payload": payload}

        return self._idempotent(idempotency_key, produce)

    def _apply_partial_acceptance(self, conn: sqlite3.Connection, commitment: sqlite3.Row,
                                  actor_id: str, accepted_qty: float,
                                  document_ref: str, note: str) -> tuple[sqlite3.Row, dict]:
        """在给定事务内执行部分验收（供单条接口与批量任务共用）。"""
        if accepted_qty <= 0:
            raise ValidationError("验收数量必须为正数")
        remaining = commitment["total_qty"] - commitment["accepted_qty"]
        if accepted_qty > remaining + 1e-9:
            raise ValidationError(
                f"累计验收数量超出总量：本次 {accepted_qty}，剩余可验收 {remaining:.4f}")
        if not document_ref:
            raise ValidationError("验收必须提供证据编号或报告链接 document_ref")
        new_accepted = commitment["accepted_qty"] + accepted_qty
        if new_accepted >= commitment["total_qty"] - 1e-9:
            new_accepted = commitment["total_qty"]
            new_status = "accepted"
        else:
            new_status = "partial_accepted"
        evidence_id = _new_id("evd")
        conn.execute(
            "INSERT INTO acceptance_evidence (id, commitment_id, accepted_qty,"
            " document_ref, note, recorded_by) VALUES (?,?,?,?,?,?)",
            (evidence_id, commitment["id"], accepted_qty, document_ref, note, actor_id))
        conn.execute(
            "UPDATE commitments SET accepted_qty=?, status=? WHERE id=?",
            (new_accepted, new_status, commitment["id"]))
        payload = {
            "commitment_id": commitment["id"],
            "item": commitment["item"],
            "accepted_qty": accepted_qty,
            "cumulative_accepted_qty": new_accepted,
            "total_qty": commitment["total_qty"],
            "evidence_id": evidence_id,
            "document_ref": document_ref,
            "note": note,
        }
        amd = self._append_amendment(
            conn, commitment["agreement_id"], "partial_acceptance", payload,
            actor_id, commitment["id"])
        return amd, payload

    def partial_acceptance(self, actor_token: str, commitment_id: str,
                           accepted_qty: float, document_ref: str, note: str = "",
                           idempotency_key: str | None = None) -> dict:
        """部分验收：必须附证据；累计不超总量；全额时状态转 accepted。"""
        actor = self._authenticate(actor_token)
        commitment = self._get_visible_commitment(commitment_id, actor)
        self._require_active_agreement(commitment["agreement_id"])
        if actor["role"] != "项目秘书处" and actor["id"] != commitment["current_owner"]:
            raise Forbidden("仅秘书处或当前负责方可登记验收")

        def produce() -> dict:
            # 事务内重读，避免并发下超额验收；事务由 _idempotent 管理
            fresh = self.conn.execute(
                "SELECT * FROM commitments WHERE id=?", (commitment_id,)).fetchone()
            amd, payload = self._apply_partial_acceptance(
                self.conn, fresh, actor["id"], float(accepted_qty),
                document_ref, note)
            return {"amendment_id": amd["id"], "seq": amd["seq"],
                    "entry_hash": amd["entry_hash"],
                    "kind": "partial_acceptance", "payload": payload}

        return self._idempotent(idempotency_key, produce)

    def terminate(self, actor_token: str, agreement_id: str, reason: str,
                  idempotency_key: str | None = None) -> dict:
        """终止协议：追加 termination 变更，全部未完成承诺派生为 terminated。"""
        actor = self._authenticate(actor_token)
        self._require_secretariat(actor["id"])
        agreement = self._require_sealed(agreement_id)
        if agreement["status"] == "terminated":
            raise Conflict("协议已处于终止状态")
        payload = {"reason": reason}

        def produce() -> dict:
            amd = self._append_amendment(
                self.conn, agreement_id, "termination", payload, actor["id"])
            self.conn.execute(
                "UPDATE commitments SET status='terminated'"
                " WHERE agreement_id=? AND status != 'accepted'",
                (agreement_id,))
            self.conn.execute(
                "UPDATE agreements SET status='terminated' WHERE id=?",
                (agreement_id,))
            return {"amendment_id": amd["id"], "seq": amd["seq"],
                    "entry_hash": amd["entry_hash"], "kind": "termination",
                    "payload": payload}

        return self._idempotent(idempotency_key, produce)

    def _apply_disbursement(self, conn: sqlite3.Connection, commitment: sqlite3.Row,
                            actor_id: str, amount: float, note: str) -> tuple[sqlite3.Row, dict]:
        if amount <= 0:
            raise ValidationError("拨付金额必须为正数")
        budget = conn.execute(
            "SELECT * FROM budget_lines WHERE commitment_id=?",
            (commitment["id"],)).fetchone()
        # 经费约束：累计拨付不得超过按验收比例对应的金额
        payable = budget["amount"] * commitment["accepted_qty"] / commitment["total_qty"]
        available = payable - budget["disbursed"]
        if amount > available + 1e-9:
            raise ValidationError(
                f"拨付超过验收比例允许额度：本次 {amount}，当前可拨 {available:.4f}"
                f"（预算 {budget['amount']}，"
                f"验收 {commitment['accepted_qty']}/{commitment['total_qty']}）")
        new_disbursed = budget["disbursed"] + amount
        conn.execute(
            "UPDATE budget_lines SET disbursed=? WHERE id=?",
            (new_disbursed, budget["id"]))
        payload = {
            "commitment_id": commitment["id"],
            "item": commitment["item"],
            "amount": amount,
            "cumulative_disbursed": new_disbursed,
            "budget_amount": budget["amount"],
            "accepted_qty": commitment["accepted_qty"],
            "total_qty": commitment["total_qty"],
            "note": note,
        }
        amd = self._append_amendment(
            conn, commitment["agreement_id"], "disbursement", payload,
            actor_id, commitment["id"])
        return amd, payload

    def disburse(self, actor_token: str, commitment_id: str, amount: float,
                 note: str = "", idempotency_key: str | None = None) -> dict:
        """经费拨付：只追加 disbursement 变更，受验收比例与预算上限双重约束。"""
        actor = self._authenticate(actor_token)
        self._require_secretariat(actor["id"])
        commitment = self._get_visible_commitment(commitment_id, actor)
        self._require_active_agreement(commitment["agreement_id"])

        def produce() -> dict:
            fresh = self.conn.execute(
                "SELECT * FROM commitments WHERE id=?", (commitment_id,)).fetchone()
            try:
                amd, payload = self._apply_disbursement(
                    self.conn, fresh, actor["id"], float(amount), note)
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"拨付超出预算上限：{exc}") from exc
            return {"amendment_id": amd["id"], "seq": amd["seq"],
                    "entry_hash": amd["entry_hash"], "kind": "disbursement",
                    "payload": payload}

        return self._idempotent(idempotency_key, produce)

    def mark_progress(self, actor_token: str, commitment_id: str,
                      idempotency_key: str | None = None) -> dict:
        """标记承诺进入执行中（批量更新的轻量操作）。"""
        actor = self._authenticate(actor_token)
        commitment = self._get_visible_commitment(commitment_id, actor)
        self._require_active_agreement(commitment["agreement_id"])
        if actor["role"] != "项目秘书处" and actor["id"] != commitment["current_owner"]:
            raise Forbidden("仅秘书处或当前负责方可更新进度")
        if commitment["status"] == "accepted":
            raise Conflict("承诺已全额验收，不能再开始执行")

        def produce() -> dict:
            fresh = self.conn.execute(
                "SELECT status FROM commitments WHERE id=?",
                (commitment_id,)).fetchone()
            if fresh["status"] == "in_progress":
                return {"commitment_id": commitment_id, "status": "in_progress",
                        "changed": False}
            self.conn.execute(
                "UPDATE commitments SET status='in_progress' WHERE id=?",
                (commitment_id,))
            return {"commitment_id": commitment_id, "status": "in_progress",
                    "changed": True}

        return self._idempotent(idempotency_key, produce)

    def mark_milestone(self, actor_token: str, milestone_id: str,
                       status: str) -> dict:
        """更新里程碑达成状态（原约定不变，仅状态派生）。"""
        actor = self._authenticate(actor_token)
        row = self._row("SELECT * FROM milestones WHERE id=?", (milestone_id,))
        if row is None:
            raise NotFound(f"里程碑不存在：{milestone_id}")
        if status not in ("pending", "met", "partial", "missed"):
            raise ValidationError("里程碑状态非法")
        if actor["role"] != "项目秘书处":
            raise Forbidden("仅秘书处可登记里程碑状态")
        self.conn.execute("UPDATE milestones SET status=? WHERE id=?",
                          (status, milestone_id))
        self.conn.commit()
        return dict(self._row("SELECT * FROM milestones WHERE id=?", (milestone_id,)))

    # ------------------------------------------------------------ 权限视图

    def _visible_commitments(self, org_id: str, agreement_id: str) -> list[sqlite3.Row]:
        """机构可见的承诺：自己负有义务的（原负责方或移交后的当前负责方）
        及其依赖的前置承诺（沿 depends_on 向上的闭包），按协议序号返回。"""
        rows = self._all(
            "SELECT * FROM commitments WHERE agreement_id=? ORDER BY seq",
            (agreement_id,))
        by_id = {r["id"]: r for r in rows}
        visible: set[str] = set()
        stack = [r["id"] for r in rows
                 if r["owner_org_id"] == org_id or r["current_owner"] == org_id]
        while stack:
            cid = stack.pop()
            if cid in visible:
                continue
            visible.add(cid)
            dep = by_id[cid]["depends_on"] if cid in by_id else None
            if dep:
                stack.append(dep)
        return [r for r in rows if r["id"] in visible]

    def _commitment_view(self, commitment_id: str) -> dict:
        r = self._row(
            "SELECT c.*, b.amount AS budget_amount, b.disbursed AS budget_disbursed"
            " FROM commitments c JOIN budget_lines b ON b.commitment_id=c.id"
            " WHERE c.id=?", (commitment_id,))
        view = dict(r)
        view["evidence"] = [
            dict(e) for e in self._all(
                "SELECT id, accepted_qty, document_ref, note, recorded_by, created_at"
                " FROM acceptance_evidence WHERE commitment_id=? ORDER BY created_at",
                (commitment_id,))
        ]
        return view

    def my_obligations(self, actor_token: str, agreement_id: str) -> dict:
        """机构视角：自己的义务及其依赖；秘书处视角为全量责任矩阵。"""
        actor = self._authenticate(actor_token)
        agreement = self._get_agreement(agreement_id)
        party = self._row(
            "SELECT 1 FROM agreement_parties WHERE agreement_id=? AND org_id=?",
            (agreement_id, actor["id"]))
        if actor["role"] != "项目秘书处" and party is None:
            raise NotFound("协议不存在或本方未参与")
        if actor["role"] == "项目秘书处":
            ids = [r["id"] for r in self._all(
                "SELECT id FROM commitments WHERE agreement_id=? ORDER BY seq",
                (agreement_id,))]
        else:
            ids = [r["id"] for r in
                   self._visible_commitments(actor["id"], agreement_id)]
        return {
            "agreement_id": agreement_id,
            "viewer": {"id": actor["id"], "name": actor["name"], "role": actor["role"]},
            "commitments": [self._commitment_view(cid) for cid in ids],
        }

    def agreement_detail(self, actor_token: str, agreement_id: str) -> dict:
        """协议详情（按权限裁剪）。秘书处可见全量及变更链。"""
        actor = self._authenticate(actor_token)
        agreement = self._get_agreement(agreement_id)
        party = self._row(
            "SELECT 1 FROM agreement_parties WHERE agreement_id=? AND org_id=?",
            (agreement_id, actor["id"]))
        if actor["role"] != "项目秘书处" and party is None:
            raise NotFound("协议不存在或本方未参与")

        parties = [
            dict(r) for r in self._all(
                "SELECT o.id, o.name, o.role FROM agreement_parties p"
                " JOIN organizations o ON o.id=p.org_id"
                " WHERE p.agreement_id=? ORDER BY o.id", (agreement_id,))
        ]
        obligations = self.my_obligations(actor_token, agreement_id)
        result = {
            "id": agreement["id"],
            "title": agreement["title"],
            "version_no": agreement["version_no"],
            "status": agreement["status"],
            "snapshot_hash": agreement["snapshot_hash"],
            "sealed_at": agreement["sealed_at"],
            "parties": parties,
            "commitments": obligations["commitments"],
        }
        if actor["role"] == "项目秘书处":
            result["milestones"] = [
                dict(r) for r in self._all(
                    "SELECT * FROM milestones WHERE agreement_id=? ORDER BY seq",
                    (agreement_id,))
            ]
            result["amendments"] = [
                {
                    "seq": r["seq"], "kind": r["kind"],
                    "commitment_id": r["commitment_id"],
                    "payload": json.loads(r["payload_json"]),
                    "requested_by": r["requested_by"],
                    "prev_hash": r["prev_hash"], "entry_hash": r["entry_hash"],
                    "created_at": r["created_at"],
                }
                for r in self._all(
                    "SELECT * FROM amendments WHERE agreement_id=? ORDER BY seq",
                    (agreement_id,))
            ]
            result["verification"] = self.verify_seal(agreement_id)
        return result

    def list_agreements(self, actor_token: str) -> list[dict]:
        actor = self._authenticate(actor_token)
        if actor["role"] == "项目秘书处":
            rows = self._all(
                "SELECT id, title, version_no, status, snapshot_hash, sealed_at"
                " FROM agreements ORDER BY created_at")
        else:
            rows = self._all(
                "SELECT a.id, a.title, a.version_no, a.status, a.snapshot_hash,"
                " a.sealed_at FROM agreements a"
                " JOIN agreement_parties p ON p.agreement_id=a.id"
                " WHERE p.org_id=? ORDER BY a.created_at", (actor["id"],))
        return [dict(r) for r in rows]

    # -------------------------------------------------------- 可恢复批量更新

    BATCH_OPS = {"progress", "partial_acceptance", "disbursement"}

    def create_batch(self, actor_token: str, agreement_id: str, items: list[dict],
                     idempotency_key: str, autorun: bool = True) -> dict:
        """创建（或按幂等键重放）批量更新任务并立即开始处理。

        autorun=False 时只落任务行（状态 running、物品 pending）不执行，
        供分片调度或崩溃恢复测试使用，之后用 resume_batch 继续。
        """
        actor = self._authenticate(actor_token)
        self._require_secretariat(actor["id"])
        agreement = self._require_sealed(agreement_id)
        if agreement["status"] == "terminated":
            raise Conflict("协议已终止，不能创建批量任务")
        if not idempotency_key:
            raise ValidationError("批量任务必须提供 idempotency_key")
        if not items:
            raise ValidationError("批量物品列表不能为空")
        commitment_ids = {
            r["id"] for r in self._all(
                "SELECT id FROM commitments WHERE agreement_id=?", (agreement_id,))
        }
        for i, item in enumerate(items):
            if item.get("op") not in self.BATCH_OPS:
                raise ValidationError(f"第 {i} 项 op 非法：{item.get('op')}")
            if not item.get("commitment_id"):
                raise ValidationError(f"第 {i} 项缺少 commitment_id")
            if item["commitment_id"] not in commitment_ids:
                raise ValidationError(
                    f"第 {i} 项承诺不属于本协议：{item['commitment_id']}")

        existing = self._row(
            "SELECT * FROM batch_jobs WHERE idempotency_key=?", (idempotency_key,))
        if existing is not None:
            # 幂等重放：返回同一任务，绝不重复执行
            result = self._batch_view(existing["id"])
            result["replayed"] = True
            return result

        job_id = _new_id("job")
        with self.conn:
            self.conn.execute(
                "INSERT INTO batch_jobs (id, agreement_id, idempotency_key,"
                " status, created_by) VALUES (?,?,?,'running',?)",
                (job_id, agreement_id, idempotency_key, actor["id"]))
            for i, item in enumerate(items):
                self.conn.execute(
                    "INSERT INTO batch_items (job_id, item_index, op, commitment_id,"
                    " payload_json, status) VALUES (?,?,?,?,?,'pending')",
                    (job_id, i, item["op"], item["commitment_id"],
                     _canonical(item.get("payload", {}))))
        if not autorun:
            return self._batch_view(job_id)
        return self.run_batch(job_id)

    def _apply_item(self, conn: sqlite3.Connection, actor_id: str,
                    item: sqlite3.Row) -> dict:
        """在物品自己的事务内执行单条变更。"""
        payload = json.loads(item["payload_json"])
        commitment = conn.execute(
            "SELECT * FROM commitments WHERE id=?",
            (item["commitment_id"],)).fetchone()
        if commitment is None:
            raise NotFound(f"承诺不存在：{item['commitment_id']}")
        agreement = conn.execute(
            "SELECT status FROM agreements WHERE id=?",
            (commitment["agreement_id"],)).fetchone()
        if agreement["status"] == "terminated" or commitment["status"] == "terminated":
            raise Conflict("协议或承诺已终止，不能执行批量变更")
        if item["op"] == "progress":
            if commitment["status"] == "accepted":
                raise Conflict("承诺已全额验收，不能更新进度")
            if commitment["status"] != "in_progress":
                conn.execute(
                    "UPDATE commitments SET status='in_progress' WHERE id=?",
                    (commitment["id"],))
            return {"commitment_id": commitment["id"], "op": "progress",
                    "status": "in_progress"}
        if item["op"] == "partial_acceptance":
            amd, amd_payload = self._apply_partial_acceptance(
                conn, commitment, actor_id,
                float(payload["accepted_qty"]),
                payload["document_ref"], payload.get("note", ""))
            return {"op": "partial_acceptance", "amendment_id": amd["id"],
                    "seq": amd["seq"], "entry_hash": amd["entry_hash"],
                    "payload": amd_payload}
        if item["op"] == "disbursement":
            amd, amd_payload = self._apply_disbursement(
                conn, commitment, actor_id, float(payload["amount"]),
                payload.get("note", ""))
            return {"op": "disbursement", "amendment_id": amd["id"],
                    "seq": amd["seq"], "entry_hash": amd["entry_hash"],
                    "payload": amd_payload}
        raise ValidationError(f"未知批量操作：{item['op']}")

    def run_batch(self, job_id: str, max_items: int | None = None,
                  crash_at: int | None = None) -> dict:
        """处理 pending 与可重试的 failed 物品。

        - 每个物品一个独立事务：任务状态/资金变更与 done 标记同事务提交，
          因此不存在「状态已改但物品未标记」的中间落盘；
        - 单项逻辑错误（超额验收/超付等）事务整体回滚（未产生任何效果），
          物品标记 failed 并继续其余物品；resume 时 failed 项会被重试，
          因为其此前的改动已随事务回滚、重试不会重复改变状态；
        - done 项永远跳过，因此重复执行/resume 不会重复改变任务或资金；
        - max_items 限制本次处理数量，便于分片执行；
        - crash_at 供测试：在指定序号的事务提交前抛出异常，模拟进程中断，
          该物品整体回滚为 pending；resume 时继续，状态不会被重复改变。
        """
        job = self._row("SELECT * FROM batch_jobs WHERE id=?", (job_id,))
        if job is None:
            raise NotFound(f"批量任务不存在：{job_id}")
        if job["status"] == "completed":
            return self._batch_view(job_id)

        pending = self._all(
            "SELECT * FROM batch_items WHERE job_id=?"
            " AND status IN ('pending','failed') ORDER BY item_index", (job_id,))
        processed = 0
        for item in pending:
            if max_items is not None and processed >= max_items:
                break
            try:
                with self.conn:  # 每物品独立事务
                    if crash_at is not None and item["item_index"] == crash_at:
                        # 模拟进程崩溃：异常触发回滚，该物品保持原状态
                        raise RuntimeError(
                            f"模拟中断：任务 {job_id} 在第 {crash_at} 项提交前崩溃")
                    result = self._apply_item(self.conn, job["created_by"], item)
                    self.conn.execute(
                        "UPDATE batch_items SET status='done', result_json=?,"
                        " error=NULL WHERE job_id=? AND item_index=?",
                        (_canonical(result), job_id, item["item_index"]))
            except LedgerError as exc:
                # 单项业务校验失败：业务改动已随事务回滚，记录失败并继续
                with self.conn:
                    self.conn.execute(
                        "UPDATE batch_items SET status='failed', error=?"
                        " WHERE job_id=? AND item_index=?",
                        (str(exc), job_id, item["item_index"]))
                continue
            processed += 1

        return self._batch_view(job_id, processed=processed)

    def resume_batch(self, actor_token: str, job_id: str,
                     max_items: int | None = None) -> dict:
        """中断后继续：done 项跳过，pending 项继续；不会重复改变任何状态。"""
        actor = self._authenticate(actor_token)
        job = self._row("SELECT * FROM batch_jobs WHERE id=?", (job_id,))
        if job is None:
            raise NotFound(f"批量任务不存在：{job_id}")
        if actor["role"] != "项目秘书处" and actor["id"] != job["created_by"]:
            raise Forbidden("仅任务创建方或秘书处可继续执行")
        return self.run_batch(job_id, max_items=max_items)

    def get_batch(self, actor_token: str, job_id: str) -> dict:
        actor = self._authenticate(actor_token)
        job = self._row("SELECT * FROM batch_jobs WHERE id=?", (job_id,))
        if job is None:
            raise NotFound(f"批量任务不存在：{job_id}")
        if actor["role"] != "项目秘书处" and actor["id"] != job["created_by"]:
            raise Forbidden("无权查看该批量任务")
        return self._batch_view(job_id)

    def _batch_view(self, job_id: str, processed: int = 0) -> dict:
        job = self._row("SELECT * FROM batch_jobs WHERE id=?", (job_id,))
        items = self._all(
            "SELECT item_index, op, commitment_id, status, result_json, error"
            " FROM batch_items WHERE job_id=? ORDER BY item_index", (job_id,))
        done = sum(1 for i in items if i["status"] == "done")
        failed = sum(1 for i in items if i["status"] == "failed")
        pending = sum(1 for i in items if i["status"] == "pending")
        if pending == 0 and job["status"] != "completed":
            new_status = "completed" if failed == 0 else "interrupted"
            self.conn.execute(
                "UPDATE batch_jobs SET status=?, finished_at=? WHERE id=?",
                (new_status, _now(), job_id))
            self.conn.commit()
            job = self._row("SELECT * FROM batch_jobs WHERE id=?", (job_id,))
        return {
            "job_id": job_id,
            "agreement_id": job["agreement_id"],
            "idempotency_key": job["idempotency_key"],
            "status": job["status"],
            "total": len(items),
            "done": done,
            "failed": failed,
            "pending": pending,
            "processed_in_this_call": processed,
            "items": [
                {
                    "item_index": i["item_index"], "op": i["op"],
                    "commitment_id": i["commitment_id"], "status": i["status"],
                    "result": json.loads(i["result_json"]) if i["result_json"] else None,
                    "error": i["error"],
                }
                for i in items
            ],
        }
