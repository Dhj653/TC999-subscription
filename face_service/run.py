"""直接启动脚本： python run.py  （等价 uvicorn app.main:app --host 127.0.0.1 --port 5002）"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

if __name__ == "__main__":
    import uvicorn

    host = os.getenv("SERVICE_HOST", "127.0.0.1")
    port = int(os.getenv("SERVICE_PORT", "5002"))
    uvicorn.run("app.main:app", host=host, port=port, log_level="info")
