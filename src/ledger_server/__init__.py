"""国际职教合作项目台账服务端。

模块布局：
- database：SQLite 连接、表结构与封存触发器；
- ledger：领域服务（签署封存、追加变更、权限视图、幂等批量）；
- httpapp：基于标准库的 HTTP/JSON 接口；
- __main__：服务启动入口。
"""
from __future__ import annotations

from .database import connect, seed_secretariat
from .ledger import (
    Conflict,
    Forbidden,
    Ledger,
    LedgerError,
    NotFound,
    ValidationError,
)

__all__ = [
    "connect",
    "seed_secretariat",
    "Ledger",
    "LedgerError",
    "ValidationError",
    "Forbidden",
    "NotFound",
    "Conflict",
]
