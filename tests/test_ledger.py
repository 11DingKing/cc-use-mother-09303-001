"""台账服务端端到端回归测试（内存 SQLite，零外部依赖）。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ledger_server.database import connect, seed_secretariat
from ledger_server.ledger import (
    Conflict,
    Forbidden,
    Ledger,
    NotFound,
    ValidationError,
)

SEK_TOKEN = "sek-secretariat-default-token"


class LedgerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        seed_secretariat(self.conn)
        self.ledger = Ledger(self.conn)
        self.cn = self.ledger.create_organization(
            SEK_TOKEN, "宁波职业技术学院", "中方院校", "cn@example.com")
        self.fo = self.ledger.create_organization(
            SEK_TOKEN, "莱茵应用技术大学", "外方院校", "de@example.com")
        self.agr = self.ledger.create_agreement(
            SEK_TOKEN, "国际职教论坛合作协议（2026 夏）")
        self.agr_id = self.agr["id"]
        self.ledger.add_party(SEK_TOKEN, self.agr_id, self.cn["id"])
        self.ledger.add_party(SEK_TOKEN, self.agr_id, self.fo["id"])

    def tearDown(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------- 测试夹具

    def _build_signed_agreement(self):
        """构建设备→师资→阶段报告三条承诺（带依赖与预算）并签署。"""
        equipment = self.ledger.add_commitment(
            SEK_TOKEN, self.agr_id, "数控实训设备一批", self.cn["id"],
            category="equipment", due_date="2026-12-31",
            total_qty=10, budget_amount=1000)
        staff = self.ledger.add_commitment(
            SEK_TOKEN, self.agr_id, "互派专业教师 4 人", self.fo["id"],
            category="staff", depends_on=equipment["id"],
            due_date="2027-03-31", total_qty=4, budget_amount=800)
        report = self.ledger.add_commitment(
            SEK_TOKEN, self.agr_id, "季度阶段报告", self.cn["id"],
            category="report", depends_on=staff["id"],
            due_date="2027-06-30", total_qty=4, budget_amount=400)
        self.ledger.add_milestone(
            SEK_TOKEN, self.agr_id, "设备到位验收", "2027-01-15")
        seal = self.ledger.seal_agreement(SEK_TOKEN, self.agr_id)
        return equipment, staff, report, seal


class SealingTests(LedgerTestCase):
    def test_seal_requires_two_parties_and_content(self) -> None:
        empty = self.ledger.create_agreement(SEK_TOKEN, "空协议")
        with self.assertRaises(ValidationError):
            self.ledger.seal_agreement(SEK_TOKEN, empty["id"])

    def test_seal_produces_stable_snapshot_hash(self) -> None:
        _, _, _, seal = self._build_signed_agreement()
        self.assertEqual(seal["status"], "sealed")
        self.assertEqual(len(seal["snapshot_hash"]), 64)
        # 重算一致
        verify = self.ledger.verify_seal(self.agr_id)
        self.assertTrue(verify["seal_intact"])
        self.assertTrue(verify["chain_intact"])

    def test_original_terms_physically_immutable_after_seal(self) -> None:
        equipment, _, _, _ = self._build_signed_agreement()
        import sqlite3
        # 直接绕过应用层改写原约定，必须被触发器物理拒绝
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE commitments SET item='篡改后的事项' WHERE id=?",
                (equipment["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE commitments SET due_date='2030-01-01' WHERE id=?",
                (equipment["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM commitments WHERE id=?",
                              (equipment["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE budget_lines SET amount=999999 WHERE commitment_id=?",
                (equipment["id"],))
        # 草稿期不能再改责任矩阵
        with self.assertRaises(Conflict):
            self.ledger.add_commitment(
                SEK_TOKEN, self.agr_id, "后期新增", self.cn["id"],
                due_date="2028-01-01")

    def test_cannot_seal_twice(self) -> None:
        self._build_signed_agreement()
        with self.assertRaises(Conflict):
            self.ledger.seal_agreement(SEK_TOKEN, self.agr_id)


class AmendmentChainTests(LedgerTestCase):
    def test_transfer_is_append_only_and_derives_owner(self) -> None:
        equipment, _, _, _ = self._build_signed_agreement()
        result = self.ledger.transfer_responsibility(
            SEK_TOKEN, equipment["id"], self.fo["id"],
            "原负责人离职，设备移交外方共同管理", idempotency_key="t1")
        self.assertEqual(result["kind"], "transfer")
        view = self.ledger._commitment_view(equipment["id"])
        # 原始负责方（签署承诺）不变；当前负责方派生为接收方
        self.assertEqual(view["owner_org_id"], self.cn["id"])
        self.assertEqual(view["current_owner"], self.fo["id"])
        self.assertEqual(view["status"], "transferred")
        # 原约定事项未被改写
        self.assertEqual(view["item"], "数控实训设备一批")

        # 变更链不可改、不可删
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE amendments SET payload_json='{}' WHERE id=?",
                              (result["amendment_id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM amendments WHERE id=?",
                              (result["amendment_id"],))
        verify = self.ledger.verify_seal(self.agr_id)
        self.assertTrue(verify["chain_intact"])

    def test_transfer_requires_party_and_rejects_same_owner(self) -> None:
        equipment, _, _, _ = self._build_signed_agreement()
        other = self.ledger.create_organization(
            SEK_TOKEN, "第三方学院", "中方院校")
        with self.assertRaises(ValidationError):
            self.ledger.transfer_responsibility(
                SEK_TOKEN, equipment["id"], other["id"], "非参与方")
        with self.assertRaises(Conflict):
            self.ledger.transfer_responsibility(
                SEK_TOKEN, equipment["id"], self.cn["id"], "同一方")

    def test_extension_keeps_original_due_date(self) -> None:
        _, staff, _, _ = self._build_signed_agreement()
        ext = self.ledger.extend_due_date(
            SEK_TOKEN, staff["id"], "2027-09-30", "签证流程延迟",
            idempotency_key="e1")
        view = self.ledger._commitment_view(staff["id"])
        self.assertEqual(view["due_date"], "2027-03-31")          # 原始不变
        self.assertEqual(view["current_due_date"], "2027-09-30")  # 派生延期
        self.assertEqual(view["status"], "delayed")
        with self.assertRaises(ValidationError):
            self.ledger.extend_due_date(
                SEK_TOKEN, staff["id"], "2027-08-01", "不能往前提")
        self.assertEqual(ext["payload"]["original_due_date"], "2027-03-31")

    def test_amendments_forbidden_before_seal(self) -> None:
        equipment = self.ledger.add_commitment(
            SEK_TOKEN, self.agr_id, "未签署设备", self.cn["id"],
            due_date="2027-01-01")
        with self.assertRaises(Conflict):
            self.ledger.transfer_responsibility(
                SEK_TOKEN, equipment["id"], self.fo["id"], "未签署不能移交")

    def test_chain_is_sequential_and_linked(self) -> None:
        equipment, _, _, _ = self._build_signed_agreement()
        self.ledger.transfer_responsibility(
            SEK_TOKEN, equipment["id"], self.fo["id"], "r1",
            idempotency_key="t1")
        self.ledger.extend_due_date(
            SEK_TOKEN, equipment["id"], "2027-02-28", "r2",
            idempotency_key="e1")
        detail = self.ledger.agreement_detail(SEK_TOKEN, self.agr_id)
        kinds = [a["kind"] for a in detail["amendments"]]
        self.assertEqual(kinds, ["transfer", "extension"])
        self.assertEqual(detail["amendments"][0]["prev_hash"], "GENESIS")
        self.assertEqual(detail["amendments"][1]["prev_hash"],
                         detail["amendments"][0]["entry_hash"])


class AcceptanceTests(LedgerTestCase):
    def test_partial_acceptance_accumulates_with_evidence(self) -> None:
        equipment, _, _, _ = self._build_signed_agreement()
        a1 = self.ledger.partial_acceptance(
            SEK_TOKEN, equipment["id"], 4, "RPT-2027-001", "首批 4 台到位",
            idempotency_key="a1")
        self.assertEqual(a1["payload"]["cumulative_accepted_qty"], 4)
        view = self.ledger._commitment_view(equipment["id"])
        self.assertEqual(view["status"], "partial_accepted")
        self.assertEqual(len(view["evidence"]), 1)

        a2 = self.ledger.partial_acceptance(
            SEK_TOKEN, equipment["id"], 6, "RPT-2027-002", "其余 6 台到位",
            idempotency_key="a2")
        self.assertEqual(a2["payload"]["cumulative_accepted_qty"], 10)
        view = self.ledger._commitment_view(equipment["id"])
        self.assertEqual(view["status"], "accepted")

    def test_over_acceptance_rejected(self) -> None:
        equipment, _, _, _ = self._build_signed_agreement()
        self.ledger.partial_acceptance(
            SEK_TOKEN, equipment["id"], 8, "RPT-1", idempotency_key="a1")
        with self.assertRaises(ValidationError):
            self.ledger.partial_acceptance(
                SEK_TOKEN, equipment["id"], 3, "RPT-2", idempotency_key="a2")

    def test_acceptance_requires_evidence(self) -> None:
        equipment, _, _, _ = self._build_signed_agreement()
        with self.assertRaises(ValidationError):
            self.ledger.partial_acceptance(
                SEK_TOKEN, equipment["id"], 1, "", idempotency_key="a1")

    def test_evidence_immutable(self) -> None:
        import sqlite3
        equipment, _, _, _ = self._build_signed_agreement()
        a1 = self.ledger.partial_acceptance(
            SEK_TOKEN, equipment["id"], 1, "RPT-1", idempotency_key="a1")
        evd_id = a1["payload"]["evidence_id"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM acceptance_evidence WHERE id=?",
                              (evd_id,))


class BudgetTests(LedgerTestCase):
    def test_disbursement_capped_by_accepted_ratio(self) -> None:
        equipment, _, _, _ = self._build_signed_agreement()
        # 未验收：一分钱不能拨
        with self.assertRaises(ValidationError):
            self.ledger.disburse(
                SEK_TOKEN, equipment["id"], 1, idempotency_key="d0")
        self.ledger.partial_acceptance(
            SEK_TOKEN, equipment["id"], 5, "RPT-1", idempotency_key="a1")
        # 验收 5/10：最多可拨 500
        self.ledger.disburse(
            SEK_TOKEN, equipment["id"], 500, "首批款", idempotency_key="d1")
        with self.assertRaises(ValidationError):
            self.ledger.disburse(
                SEK_TOKEN, equipment["id"], 0.01, idempotency_key="d2")
        self.ledger.partial_acceptance(
            SEK_TOKEN, equipment["id"], 5, "RPT-2", idempotency_key="a2")
        self.ledger.disburse(
            SEK_TOKEN, equipment["id"], 500, "尾款", idempotency_key="d3")
        view = self.ledger._commitment_view(equipment["id"])
        self.assertEqual(view["budget_disbursed"], 1000)
        self.assertEqual(view["budget_amount"], 1000)


class TerminationTests(LedgerTestCase):
    def test_termination_append_only_and_blocks_further_changes(self) -> None:
        equipment, staff, report, _ = self._build_signed_agreement()
        self.ledger.partial_acceptance(
            SEK_TOKEN, equipment["id"], 10, "RPT-1", idempotency_key="a1")
        term = self.ledger.terminate(
            SEK_TOKEN, self.agr_id, "合作终止", idempotency_key="x1")
        self.assertEqual(term["kind"], "termination")
        # 已全额验收的保持 accepted；其余派生为 terminated
        self.assertEqual(self.ledger._commitment_view(equipment["id"])["status"],
                         "accepted")
        self.assertEqual(self.ledger._commitment_view(staff["id"])["status"],
                         "terminated")
        with self.assertRaises(Conflict):
            self.ledger.transfer_responsibility(
                SEK_TOKEN, staff["id"], self.cn["id"], "终止后不能移交")
        with self.assertRaises(Conflict):
            self.ledger.partial_acceptance(
                SEK_TOKEN, staff["id"], 1, "RPT-9", idempotency_key="a9")
        # 终止后原约定仍被物理封存
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE commitments SET item='x' WHERE id=?",
                              (staff["id"],))
        with self.assertRaises(Conflict):
            self.ledger.terminate(SEK_TOKEN, self.agr_id, "重复终止")

    def test_termination_freezes_even_fully_accepted_commitments(self) -> None:
        equipment, _, _, _ = self._build_signed_agreement()
        self.ledger.partial_acceptance(
            SEK_TOKEN, equipment["id"], 10, "RPT-1", idempotency_key="a1")
        self.ledger.terminate(SEK_TOKEN, self.agr_id, "终止", idempotency_key="x1")
        # 即使承诺本身保持 accepted，协议终止后也不能再拨付/延期/验收
        with self.assertRaises(Conflict):
            self.ledger.disburse(
                SEK_TOKEN, equipment["id"], 1, idempotency_key="d-after")
        with self.assertRaises(Conflict):
            self.ledger.extend_due_date(
                SEK_TOKEN, equipment["id"], "2030-01-01", "终止后",
                idempotency_key="e-after")

    def test_extension_rejected_for_accepted_commitment(self) -> None:
        equipment, _, _, _ = self._build_signed_agreement()
        self.ledger.partial_acceptance(
            SEK_TOKEN, equipment["id"], 10, "RPT-1", idempotency_key="a1")
        with self.assertRaises(Conflict):
            self.ledger.extend_due_date(
                SEK_TOKEN, equipment["id"], "2030-01-01", "已验收无需延期",
                idempotency_key="e1")


class PermissionTests(LedgerTestCase):
    def test_org_sees_only_own_obligations_and_dependencies(self) -> None:
        equipment, staff, report, _ = self._build_signed_agreement()
        # 外方负责 staff；staff 依赖 equipment → 外方应看到 staff 与 equipment，
        # 但看不到仅中方负责的 report
        view = self.ledger.my_obligations(self.fo["auth_token"], self.agr_id)
        visible = {c["id"] for c in view["commitments"]}
        self.assertIn(staff["id"], visible)
        self.assertIn(equipment["id"], visible)  # 依赖的前置承诺
        self.assertNotIn(report["id"], visible)

        # 中方负责 equipment 与 report；report→staff→equipment，全链可见
        cn_view = self.ledger.my_obligations(self.cn["auth_token"], self.agr_id)
        cn_visible = {c["id"] for c in cn_view["commitments"]}
        self.assertEqual(cn_visible, {equipment["id"], staff["id"], report["id"]})

    def test_org_cannot_access_unrelated_commitment(self) -> None:
        equipment, staff, report, _ = self._build_signed_agreement()
        # 外方对 report 无义务也无依赖 → 404（不暴露存在性）
        with self.assertRaises(NotFound):
            self.ledger.partial_acceptance(
                self.fo["auth_token"], report["id"], 1, "RPT-X",
                idempotency_key="x")

    def test_org_cannot_administer(self) -> None:
        with self.assertRaises(Forbidden):
            self.ledger.create_organization(
                self.cn["auth_token"], "越权机构", "中方院校")
        with self.assertRaises(Forbidden):
            self.ledger.list_organizations(self.fo["auth_token"])

    def test_secretariat_sees_everything_and_chain(self) -> None:
        self._build_signed_agreement()
        detail = self.ledger.agreement_detail(SEK_TOKEN, self.agr_id)
        self.assertEqual(len(detail["commitments"]), 3)
        self.assertIn("amendments", detail)
        self.assertIn("verification", detail)
        # 院校视角不含变更链
        org_detail = self.ledger.agreement_detail(
            self.fo["auth_token"], self.agr_id)
        self.assertNotIn("amendments", org_detail)

    def test_non_party_sees_nothing(self) -> None:
        self._build_signed_agreement()
        outsider = self.ledger.create_organization(
            SEK_TOKEN, "局外学院", "中方院校")
        with self.assertRaises(NotFound):
            self.ledger.my_obligations(outsider["auth_token"], self.agr_id)
        self.assertEqual(
            self.ledger.list_agreements(outsider["auth_token"]), [])


class IdempotencyTests(LedgerTestCase):
    def test_same_key_replays_without_double_effect(self) -> None:
        equipment, _, _, _ = self._build_signed_agreement()
        first = self.ledger.partial_acceptance(
            SEK_TOKEN, equipment["id"], 3, "RPT-1", idempotency_key="k1")
        second = self.ledger.partial_acceptance(
            SEK_TOKEN, equipment["id"], 3, "RPT-1", idempotency_key="k1")
        self.assertTrue(second["replayed"])
        self.assertEqual(second["amendment_id"], first["amendment_id"])
        view = self.ledger._commitment_view(equipment["id"])
        self.assertEqual(view["accepted_qty"], 3)  # 没有累加成 6
        self.assertEqual(len(view["evidence"]), 1)

    def test_disbursement_idempotent(self) -> None:
        equipment, _, _, _ = self._build_signed_agreement()
        self.ledger.partial_acceptance(
            SEK_TOKEN, equipment["id"], 10, "RPT-1", idempotency_key="a")
        self.ledger.disburse(
            SEK_TOKEN, equipment["id"], 200, idempotency_key="d")
        replay = self.ledger.disburse(
            SEK_TOKEN, equipment["id"], 200, idempotency_key="d")
        self.assertTrue(replay["replayed"])
        self.assertEqual(
            self.ledger._commitment_view(equipment["id"])["budget_disbursed"], 200)


class BatchTests(LedgerTestCase):
    def _three_signed_commitments(self):
        c1 = self.ledger.add_commitment(
            SEK_TOKEN, self.agr_id, "设备 A", self.cn["id"],
            category="equipment", due_date="2027-01-01",
            total_qty=10, budget_amount=1000)
        c2 = self.ledger.add_commitment(
            SEK_TOKEN, self.agr_id, "师资 B", self.fo["id"],
            category="staff", depends_on=c1["id"], due_date="2027-02-01",
            total_qty=2, budget_amount=500)
        c3 = self.ledger.add_commitment(
            SEK_TOKEN, self.agr_id, "报告 C", self.cn["id"],
            category="report", depends_on=c2["id"], due_date="2027-03-01",
            total_qty=1, budget_amount=100)
        self.ledger.seal_agreement(SEK_TOKEN, self.agr_id)
        return c1, c2, c3

    def test_batch_runs_all_items(self) -> None:
        c1, c2, c3 = self._three_signed_commitments()
        job = self.ledger.create_batch(SEK_TOKEN, self.agr_id, [
            {"op": "progress", "commitment_id": c1["id"], "payload": {}},
            {"op": "partial_acceptance", "commitment_id": c1["id"],
             "payload": {"accepted_qty": 10, "document_ref": "EV-1"}},
            {"op": "disbursement", "commitment_id": c1["id"],
             "payload": {"amount": 1000}},
        ], idempotency_key="batch-1")
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["done"], 3)
        view = self.ledger._commitment_view(c1["id"])
        self.assertEqual(view["status"], "accepted")
        self.assertEqual(view["budget_disbursed"], 1000)

    def test_batch_interrupted_then_resumed_without_double_effects(self) -> None:
        c1, c2, c3 = self._three_signed_commitments()
        # 先给 c1 全额验收，使第 3 项（超付）成为业务失败项
        self.ledger.partial_acceptance(
            SEK_TOKEN, c1["id"], 10, "EV-0", idempotency_key="pre")
        job = self.ledger.create_batch(SEK_TOKEN, self.agr_id, [
            {"op": "progress", "commitment_id": c2["id"], "payload": {}},
            {"op": "progress", "commitment_id": c3["id"], "payload": {}},
            {"op": "disbursement", "commitment_id": c2["id"],
             "payload": {"amount": 100}},   # c2 未验收 → 业务失败
        ], idempotency_key="batch-2")
        self.assertEqual(job["status"], "interrupted")
        self.assertEqual(job["done"], 2)
        self.assertEqual(job["failed"], 1)
        self.assertTrue(job["items"][2]["error"])

        # 完成前置条件后 resume；前两项保持 done 不会被重复执行
        self.ledger.partial_acceptance(
            SEK_TOKEN, c2["id"], 2, "EV-2", idempotency_key="a2")
        resumed = self.ledger.resume_batch(SEK_TOKEN, job["job_id"])
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["done"], 3)
        # 资金状态只变化一次
        self.assertEqual(
            self.ledger._commitment_view(c2["id"])["budget_disbursed"], 100)
        # 再 resume 是空操作
        again = self.ledger.resume_batch(SEK_TOKEN, job["job_id"])
        self.assertEqual(again["processed_in_this_call"], 0)

    def test_batch_crash_midway_leaves_pending_and_resumes_cleanly(self) -> None:
        c1, c2, c3 = self._three_signed_commitments()
        # 只建任务行不执行，随后在第 2 项（index=1）提交前注入崩溃
        crashy = self.ledger.create_batch(SEK_TOKEN, self.agr_id, [
            {"op": "progress", "commitment_id": c1["id"], "payload": {}},
            {"op": "progress", "commitment_id": c2["id"], "payload": {}},
            {"op": "progress", "commitment_id": c3["id"], "payload": {}},
        ], idempotency_key="batch-crash", autorun=False)
        with self.assertRaises(RuntimeError):
            self.ledger.run_batch(crashy["job_id"], crash_at=1)
        # 第 0 项已落盘 done；第 1 项整体回滚为 pending
        state = self.ledger.get_batch(SEK_TOKEN, crashy["job_id"])
        self.assertEqual(state["items"][0]["status"], "done")
        self.assertEqual(state["items"][1]["status"], "pending")
        # c2 的状态未被崩溃项改变（仍为 pending，不是 in_progress）
        self.assertEqual(
            self.ledger._commitment_view(c2["id"])["status"], "pending")
        # resume 后全部完成，且无重复
        resumed = self.ledger.resume_batch(SEK_TOKEN, crashy["job_id"])
        self.assertEqual(resumed["status"], "completed")
        statuses = [i["status"] for i in resumed["items"]]
        self.assertEqual(statuses, ["done", "done", "done"])

    def test_batch_idempotency_key_replays_same_job(self) -> None:
        c1, _, _ = self._three_signed_commitments()
        items = [{"op": "progress", "commitment_id": c1["id"], "payload": {}}]
        first = self.ledger.create_batch(
            SEK_TOKEN, self.agr_id, items, idempotency_key="dup-key")
        second = self.ledger.create_batch(
            SEK_TOKEN, self.agr_id, items, idempotency_key="dup-key")
        self.assertTrue(second["replayed"])
        self.assertEqual(first["job_id"], second["job_id"])

    def test_batch_requires_secretariat(self) -> None:
        c1, _, _ = self._three_signed_commitments()
        with self.assertRaises(Forbidden):
            self.ledger.create_batch(
                self.fo["auth_token"], self.agr_id,
                [{"op": "progress", "commitment_id": c1["id"], "payload": {}}],
                idempotency_key="org-batch")


class PersistenceTests(unittest.TestCase):
    def test_state_survives_connection_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ledger.db")
            conn = connect(path)
            seed_secretariat(conn)
            ledger = Ledger(conn)
            cn = ledger.create_organization(
                SEK_TOKEN, "中方院校 X", "中方院校")
            fo = ledger.create_organization(
                SEK_TOKEN, "外方院校 Y", "外方院校")
            agr = ledger.create_agreement(SEK_TOKEN, "持久化协议")
            ledger.add_party(SEK_TOKEN, agr["id"], cn["id"])
            ledger.add_party(SEK_TOKEN, agr["id"], fo["id"])
            com = ledger.add_commitment(
                SEK_TOKEN, agr["id"], "持久设备", cn["id"],
                due_date="2027-01-01", total_qty=2, budget_amount=200)
            ledger.seal_agreement(SEK_TOKEN, agr["id"])
            conn.close()

            # 重新打开：封存与派生状态均应保留，且原约定仍不可改
            conn2 = connect(path)
            ledger2 = Ledger(conn2)
            view = ledger2._commitment_view(com["id"])
            self.assertEqual(view["item"], "持久设备")
            import sqlite3
            with self.assertRaises(sqlite3.IntegrityError):
                conn2.execute("UPDATE commitments SET item='z' WHERE id=?",
                              (com["id"],))
            conn2.close()


if __name__ == "__main__":
    unittest.main()
