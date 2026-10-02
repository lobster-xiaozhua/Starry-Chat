"""启动入口。

开发：python run.py —— 监听 127.0.0.1:8000，reload=True，CORS=*，限流关闭。
生产：APP_ENV=production python run.py —— 监听 0.0.0.0:8000，reload=False，
      信任代理头，worker 数 = CPU 核心数，限流开启。

注意：生产环境不要使用 --reload，请用 systemd / supervisor 管理进程（见 README）。
"""

import multiprocessing
import os

import uvicorn

from app.config import settings


def main() -> None:
    if settings.is_production:
        workers = settings.effective_workers
        uvicorn.run(
            "app.main:app",
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
            workers=workers,
            reload=False,
            proxy_headers=settings.proxy_headers,
            forwarded_allow_ips=settings.forwarded_allow_ips,
            log_level=settings.log_level.lower(),
            access_log=True,
        )
    else:
        uvicorn.run(
            "app.main:app",
            host="127.0.0.1",
            port=8000,
            reload=True,
            log_level=settings.log_level.lower(),
        )


if __name__ == "__main__":
    main()
