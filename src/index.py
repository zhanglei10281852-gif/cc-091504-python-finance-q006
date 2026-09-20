from __future__ import annotations

import os

from app import create_server


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    db_path = os.getenv("SERVERDB_DB", ".runtime/db.json")
    server = create_server(host, port, db_path)
    print(f"投研假设协作服务监听 {host}:{port}，持久化文件 {db_path}")
    server.serve_forever()


if __name__ == "__main__":
    main()
