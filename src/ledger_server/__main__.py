"""服务启动入口：``python -m ledger_server``。

环境变量：
- LEDGER_DB：SQLite 数据库路径（默认 ledger.db）
- LEDGER_HOST：监听地址（默认 127.0.0.1）
- LEDGER_PORT：监听端口（默认 8080）
"""
from __future__ import annotations

import os

from .database import seed_secretariat, connect
from .httpapp import serve


def main() -> None:
    db_path = os.environ.get("LEDGER_DB", "ledger.db")
    host = os.environ.get("LEDGER_HOST", "127.0.0.1")
    port = int(os.environ.get("LEDGER_PORT", "8080"))

    conn = connect(db_path)
    secretariat_id = seed_secretariat(conn)
    conn.close()

    httpd = serve(db_path, host=host, port=port, verbose=True)
    print(f"国际职教合作项目台账服务已启动：http://{host}:{port}")
    print(f"数据库：{db_path}；秘书处机构 ID：{secretariat_id}")
    print("秘书处默认令牌：sek-secretariat-default-token（生产环境请更换）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n服务停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
