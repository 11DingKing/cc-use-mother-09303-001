"""基于标准库 http.server 的 HTTP/JSON 接口。

鉴权：所有 /api 请求需携带 ``Authorization: Bearer <token>``
（或 ``X-Auth-Token``），令牌在秘书处登记机构时颁发。

接口一览（均返回 JSON；写操作为 POST）：
- GET  /health
- POST /api/orgs                         秘书处：登记机构
- GET  /api/orgs                         秘书处：机构名录
- GET  /api/me                           查看本方身份
- POST /api/agreements                   秘书处：起草协议
- GET  /api/agreements                   协议列表（按权限裁剪）
- GET  /api/agreements/{id}              协议详情（秘书处含变更链与校验）
- POST /api/agreements/{id}/parties      秘书处：加入参与方
- POST /api/agreements/{id}/commitments  秘书处：追加责任矩阵承诺（草稿期）
- POST /api/agreements/{id}/milestones   秘书处：追加里程碑（草稿期）
- POST /api/agreements/{id}/seal         秘书处：签署封存
- GET  /api/agreements/{id}/obligations  本方义务与依赖视图
- POST /api/agreements/{id}/terminate    秘书处：终止（追加变更）
- POST /api/commitments/{id}/transfers       责任移交
- POST /api/commitments/{id}/extensions      延期
- POST /api/commitments/{id}/acceptances     部分验收（附证据）
- POST /api/commitments/{id}/disbursements   经费拨付
- POST /api/commitments/{id}/progress        标记执行中
- POST /api/milestones/{id}                  秘书处：里程碑状态
- POST /api/agreements/{id}/batches          秘书处：创建批量任务（需幂等键）
- GET  /api/batches/{id}                     批量任务状态
- POST /api/batches/{id}/resume              中断后继续
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .ledger import (
    Conflict,
    Forbidden,
    Ledger,
    LedgerError,
    NotFound,
    ValidationError,
)

_UUID = r"[A-Za-z0-9_-]+"


# (method, regex, name)
ROUTES = [
    ("GET",  re.compile(r"^/health$"), "health"),
    ("POST", re.compile(r"^/api/orgs$"), "orgs_create"),
    ("GET",  re.compile(r"^/api/orgs$"), "orgs_list"),
    ("GET",  re.compile(r"^/api/me$"), "me"),
    ("POST", re.compile(r"^/api/agreements$"), "agreements_create"),
    ("GET",  re.compile(r"^/api/agreements$"), "agreements_list"),
    ("POST", re.compile(r"^/api/agreements/(?P<agr>[A-Za-z0-9_-]+)/parties$"), "parties_add"),
    ("POST", re.compile(r"^/api/agreements/(?P<agr>[A-Za-z0-9_-]+)/commitments$"),
     "commitments_add"),
    ("POST", re.compile(r"^/api/agreements/(?P<agr>[A-Za-z0-9_-]+)/milestones$"),
     "milestones_add"),
    ("POST", re.compile(r"^/api/agreements/(?P<agr>[A-Za-z0-9_-]+)/seal$"), "agreement_seal"),
    ("GET",  re.compile(r"^/api/agreements/(?P<agr>[A-Za-z0-9_-]+)/obligations$"),
     "obligations"),
    ("POST", re.compile(r"^/api/agreements/(?P<agr>[A-Za-z0-9_-]+)/terminate$"),
     "agreement_terminate"),
    ("POST", re.compile(r"^/api/agreements/(?P<agr>[A-Za-z0-9_-]+)/batches$"), "batches_create"),
    ("GET",  re.compile(r"^/api/agreements/(?P<agr>[A-Za-z0-9_-]+)$"), "agreement_detail"),
    ("POST", re.compile(rf"^/api/commitments/(?P<com>{_UUID})/transfers$"),
     "transfer"),
    ("POST", re.compile(rf"^/api/commitments/(?P<com>{_UUID})/extensions$"),
     "extension"),
    ("POST", re.compile(rf"^/api/commitments/(?P<com>{_UUID})/acceptances$"),
     "acceptance"),
    ("POST", re.compile(rf"^/api/commitments/(?P<com>{_UUID})/disbursements$"),
     "disbursement"),
    ("POST", re.compile(rf"^/api/commitments/(?P<com>{_UUID})/progress$"),
     "progress"),
    ("POST", re.compile(r"^/api/milestones/(?P<mil>[A-Za-z0-9_-]+)$"), "milestone_update"),
    ("POST", re.compile(r"^/api/batches/(?P<job>[A-Za-z0-9_-]+)/resume$"), "batch_resume"),
    ("GET",  re.compile(r"^/api/batches/(?P<job>[A-Za-z0-9_-]+)$"), "batch_get"),
]


class LedgerHandler(BaseHTTPRequestHandler):
    server_version = "LedgerServer/1.0"

    def setup(self) -> None:
        super().setup()
        # SQLite 连接不能跨线程共享：每个请求用自己的连接
        self._db_conn = self.server.ledger_factory()
        self._ledger = Ledger(self._db_conn)

    def finish(self) -> None:
        try:
            self._db_conn.close()
        finally:
            super().finish()

    @property
    def ledger(self) -> Ledger:
        return self._ledger

    def log_message(self, fmt: str, *args) -> None:  # 安静日志
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------ 响应工具

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return data

    def _token(self) -> str:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        token = self.headers.get("X-Auth-Token", "").strip()
        if token:
            return token
        raise Forbidden("缺少 Authorization: Bearer 令牌")

    # ------------------------------------------------------------ 分派

    def _dispatch(self, method: str) -> None:
        path = urlsplit(self.path).path
        try:
            if path == "/health":
                self._send_json({"status": "ok"})
                return
            data = self._read_json() if method == "POST" else {}
            token = self._token()
            for m, pattern, name in ROUTES:
                if m != method:
                    continue
                match = pattern.match(path)
                if match:
                    self._handle(name, match.groupdict(), token, data)
                    return
            self._send_json({"error": "not_found", "message": f"无此接口：{method} {path}"},
                            status=404)
        except Forbidden as exc:
            self._send_json({"error": "forbidden", "message": str(exc)}, status=403)
        except NotFound as exc:
            self._send_json({"error": "not_found", "message": str(exc)}, status=404)
        except ValidationError as exc:
            self._send_json({"error": "validation", "message": str(exc)}, status=400)
        except Conflict as exc:
            self._send_json({"error": "conflict", "message": str(exc)}, status=409)
        except LedgerError as exc:
            self._send_json({"error": "ledger", "message": str(exc)}, status=400)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    # ------------------------------------------------------------ 业务路由

    def _handle(self, name: str, args: dict, token: str, data: dict) -> None:
        ledger = self.ledger
        g = args

        if name == "orgs_create":
            self._send_json(ledger.create_organization(
                token, data["name"], data["role"], data.get("contact", "")),
                status=201)
        elif name == "orgs_list":
            self._send_json({"organizations": ledger.list_organizations(token)})
        elif name == "me":
            org = ledger.whoami(token)
            org.pop("auth_token", None)
            self._send_json(org)
        elif name == "agreements_create":
            self._send_json(ledger.create_agreement(
                token, data["title"], data.get("version_no", 1)), status=201)
        elif name == "agreements_list":
            self._send_json({"agreements": ledger.list_agreements(token)})
        elif name == "agreement_detail":
            self._send_json(ledger.agreement_detail(token, g["agr"]))
        elif name == "parties_add":
            self._send_json(ledger.add_party(token, g["agr"], data["org_id"]),
                            status=201)
        elif name == "commitments_add":
            self._send_json(ledger.add_commitment(
                token, g["agr"], data["item"], data["owner_org_id"],
                category=data.get("category", "other"),
                depends_on=data.get("depends_on"),
                due_date=data.get("due_date"), detail=data.get("detail", ""),
                total_qty=float(data.get("total_qty", 1)),
                budget_amount=float(data.get("budget_amount", 0))), status=201)
        elif name == "milestones_add":
            self._send_json(ledger.add_milestone(
                token, g["agr"], data["name"], data["due_date"]), status=201)
        elif name == "agreement_seal":
            self._send_json(ledger.seal_agreement(token, g["agr"]), status=200)
        elif name == "obligations":
            self._send_json(ledger.my_obligations(token, g["agr"]))
        elif name == "agreement_terminate":
            self._send_json(ledger.terminate(
                token, g["agr"], data.get("reason", ""),
                data.get("idempotency_key")))
        elif name == "transfer":
            self._send_json(ledger.transfer_responsibility(
                token, g["com"], data["new_owner_org_id"], data.get("reason", ""),
                data.get("idempotency_key")), status=201)
        elif name == "extension":
            self._send_json(ledger.extend_due_date(
                token, g["com"], data["new_due_date"], data.get("reason", ""),
                data.get("idempotency_key")), status=201)
        elif name == "acceptance":
            self._send_json(ledger.partial_acceptance(
                token, g["com"], float(data["accepted_qty"]),
                data["document_ref"], data.get("note", ""),
                data.get("idempotency_key")), status=201)
        elif name == "disbursement":
            self._send_json(ledger.disburse(
                token, g["com"], float(data["amount"]), data.get("note", ""),
                data.get("idempotency_key")), status=201)
        elif name == "progress":
            self._send_json(ledger.mark_progress(
                token, g["com"], data.get("idempotency_key")))
        elif name == "milestone_update":
            self._send_json(ledger.mark_milestone(token, g["mil"], data["status"]))
        elif name == "batches_create":
            self._send_json(ledger.create_batch(
                token, g["agr"], data["items"], data["idempotency_key"]),
                status=201)
        elif name == "batch_get":
            self._send_json(ledger.get_batch(token, g["job"]))
        elif name == "batch_resume":
            self._send_json(ledger.resume_batch(
                token, g["job"], data.get("max_items")))
        else:  # pragma: no cover
            self._send_json({"error": "not_found", "message": name}, status=404)


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8080,
          verbose: bool = False) -> ThreadingHTTPServer:
    """创建并返回 HTTP 服务（调用方负责 serve_forever）。

    启动时初始化一次表结构；每个请求线程从连接池工厂获取独立 SQLite 连接，
    因为 SQLite 连接对象不能跨线程共享。
    """
    from .database import connect

    connect(db_path)  # 启动时建表一次

    def ledger_factory() -> "sqlite3.Connection":
        import sqlite3
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    httpd = ThreadingHTTPServer((host, port), LedgerHandler)
    httpd.verbose = verbose
    httpd.ledger_factory = ledger_factory
    return httpd
