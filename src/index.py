from __future__ import annotations

import os
from pathlib import Path

from app import create_server
from service import ResearchService
from store import Store

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    state_path = os.getenv("STATE_PATH", str(ROOT / ".runtime" / "state.json"))
    reference_path = os.getenv("REFERENCE_PATH", str(ROOT / "reference" / "domain.json"))
    service = ResearchService(Store(state_path), reference_path)
    server = create_server(host, port, service)
    server.serve_forever()


if __name__ == "__main__":
    main()
