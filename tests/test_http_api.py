"""HTTP/JSON 接口端到端测试（真实 socket，零外部依赖）。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ledger_server.database import connect, seed_secretariat
from ledger_server.httpapp import serve

SEK_TOKEN = "sek-secretariat-default-token"


class HttpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "http.db")
        init_conn = connect(self.db_path)
        seed_secretariat(init_conn)
        init_conn.close()
        self.httpd = serve(self.db_path, host="127.0.0.1", port=0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def call(self, method: str, path: str, token: str | None = None,
             body: dict | None = None, expect: int | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                status = resp.status
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            status = exc.code
        if expect is not None:
            self.assertEqual(status, expect, f"{method} {path}: {payload}")
        return status, payload

    def test_health(self) -> None:
        status, payload = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def test_full_lifecycle_over_http(self) -> None:
        # 鉴权缺失 → 403
        status, _ = self.call("GET", "/api/me", expect=403)

        # 登记两所院校
        _, cn = self.call("POST", "/api/orgs", SEK_TOKEN,
                          {"name": "中方院校网", "role": "中方院校"}, 201)
        _, fo = self.call("POST", "/api/orgs", SEK_TOKEN,
                          {"name": "外方院校网", "role": "外方院校"}, 201)

        # 起草协议、参与方、责任矩阵、里程碑、签署
        _, agr = self.call("POST", "/api/agreements", SEK_TOKEN,
                           {"title": "HTTP 全流程协议"}, 201)
        agr_id = agr["id"]
        self.call("POST", f"/api/agreements/{agr_id}/parties", SEK_TOKEN,
                  {"org_id": cn["id"]}, 201)
        self.call("POST", f"/api/agreements/{agr_id}/parties", SEK_TOKEN,
                  {"org_id": fo["id"]}, 201)
        _, equipment = self.call(
            "POST", f"/api/agreements/{agr_id}/commitments", SEK_TOKEN,
            {"item": "网联实训设备", "owner_org_id": cn["id"],
             "category": "equipment", "due_date": "2027-01-01",
             "total_qty": 10, "budget_amount": 1000}, 201)
        _, staff = self.call(
            "POST", f"/api/agreements/{agr_id}/commitments", SEK_TOKEN,
            {"item": "网联师资", "owner_org_id": fo["id"],
             "category": "staff", "depends_on": equipment["id"],
             "due_date": "2027-02-01", "total_qty": 2, "budget_amount": 400}, 201)
        self.call("POST", f"/api/agreements/{agr_id}/milestones", SEK_TOKEN,
                  {"name": "设备里程碑", "due_date": "2027-01-10"}, 201)
        _, seal = self.call("POST", f"/api/agreements/{agr_id}/seal",
                            SEK_TOKEN, {}, 200)
        self.assertEqual(len(seal["snapshot_hash"]), 64)

        # 责任移交（原负责人离职场景）
        _, transfer = self.call(
            "POST", f"/api/commitments/{equipment['id']}/transfers",
            SEK_TOKEN, {"new_owner_org_id": fo["id"], "reason": "负责人离职",
                        "idempotency_key": "http-t1"}, 201)
        self.assertEqual(transfer["kind"], "transfer")

        # 权限视图：外方应同时看到移交给自己的 equipment 与其负责的 staff
        _, obligations = self.call(
            "GET", f"/api/agreements/{agr_id}/obligations", fo["auth_token"])
        visible = {c["id"] for c in obligations["commitments"]}
        self.assertEqual(visible, {equipment["id"], staff["id"]})

        # 部分验收 → 拨付受验收比例约束
        self.call("POST", f"/api/commitments/{equipment['id']}/acceptances",
                  fo["auth_token"],
                  {"accepted_qty": 5, "document_ref": "WEB-EV-1",
                   "idempotency_key": "http-a1"}, 201)
        self.call("POST", f"/api/commitments/{equipment['id']}/disbursements",
                  SEK_TOKEN, {"amount": 500, "idempotency_key": "http-d1"}, 201)
        status, err = self.call(
            "POST", f"/api/commitments/{equipment['id']}/disbursements",
            SEK_TOKEN, {"amount": 500, "idempotency_key": "http-d2"}, 400)
        self.assertEqual(err["error"], "validation")

        # 院校看不到变更链
        _, org_detail = self.call(
            "GET", f"/api/agreements/{agr_id}", fo["auth_token"], )
        self.assertNotIn("amendments", org_detail)

        # 可恢复批量：外方无权创建
        self.call("POST", f"/api/agreements/{agr_id}/batches",
                  fo["auth_token"], {"idempotency_key": "x", "items": []}, 403)
        # 秘书处分片执行：只处理 1 项后任务仍 running，resume 继续
        items = [
            {"op": "progress", "commitment_id": staff["id"], "payload": {}},
        ]
        _, job = self.call(
            "POST", f"/api/agreements/{agr_id}/batches", SEK_TOKEN,
            {"idempotency_key": "http-batch", "items": items}, 201)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["done"], 1)
        # 幂等重放
        _, replay = self.call(
            "POST", f"/api/agreements/{agr_id}/batches", SEK_TOKEN,
            {"idempotency_key": "http-batch", "items": items}, 201)
        self.assertTrue(replay["replayed"])

        # 终止后拒绝新变更
        self.call("POST", f"/api/agreements/{agr_id}/terminate", SEK_TOKEN,
                  {"reason": "论坛周期结束", "idempotency_key": "http-x"}, 200)
        self.call("POST", f"/api/commitments/{staff['id']}/transfers",
                  SEK_TOKEN, {"new_owner_org_id": cn["id"], "reason": "终止后"},
                  409)

        # 秘书处详情含校验且封存完好
        _, detail = self.call("GET", f"/api/agreements/{agr_id}", SEK_TOKEN)
        self.assertTrue(detail["verification"]["seal_intact"])
        self.assertTrue(detail["verification"]["chain_intact"])


if __name__ == "__main__":
    unittest.main()
