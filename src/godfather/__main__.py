"""命令行入口：启动 API 服务或独立 worker。"""
from __future__ import annotations

import argparse
import sys
import time

from .settings import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="godfather", description="Godfather 本地训练工作台")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动 Web 服务")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--lan", action="store_true", help="监听局域网（手机批注用）")
    serve.add_argument("--reload", action="store_true")

    worker = sub.add_parser("worker", help="启动独立 worker 进程")
    worker.add_argument("--concurrency", type=int, default=1)

    sub.add_parser("preflight", help="环境预检：LaTeX / Pandoc / Ollama")

    args = parser.parse_args(argv)
    settings = load_settings()

    if args.command == "preflight":
        from .taskpacks.latex import compiler as latex_compiler
        from .taskpacks.latex import pandoc_base

        engine, message = latex_compiler.detect_engine(settings)
        print("== 平台 ==")
        print(latex_compiler.platform_info())
        print("== LaTeX ==")
        print(message)
        if engine:
            print("版本：", latex_compiler.engine_version(engine))
        print("== Pandoc ==")
        print("可用：", pandoc_base.is_available(settings), pandoc_base.pandoc_version(settings))
        print("== Ollama ==")
        from .providers.registry import ProviderRegistry

        for item in ProviderRegistry(settings).health():
            print(item)
        return 0 if engine else 1

    if args.command == "worker":
        from .app import register_job_handlers
        from .context import AppContext

        ctx = AppContext.create(settings)
        register_job_handlers(ctx)
        concurrency = max(1, int(getattr(args, "concurrency", 1) or 1))
        print(f"worker 启动，模式={settings.worker.mode}，并发={concurrency}")
        if concurrency == 1:
            ctx.queue.worker_loop()
        else:
            import threading

            stop = threading.Event()
            threads = [
                threading.Thread(target=ctx.queue.worker_loop, kwargs={"stop_event": stop}, daemon=True)
                for _ in range(concurrency)
            ]
            for thread in threads:
                thread.start()
            try:
                while True:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                stop.set()
        return 0

    # 默认 serve
    import uvicorn

    host = args.host or settings.server.host
    port = args.port or settings.server.port
    if args.lan or settings.server.lan_bind:
        host = settings.server.lan_host
        # 监听局域网时必须启用配对凭证，否则同网段任意设备都能读写项目
        settings.server.lan_bind = True
    if host not in ("127.0.0.1", "localhost", "::1", ""):
        # 显式指定非回环地址同样必须启用配对
        settings.server.lan_bind = True
    settings.server.host = host
    print(f"Godfather → http://{host}:{port}")
    if settings.server.lan_bind:
        print("局域网访问已开启：手机需要输入**对应项目**的配对码（网页右上角可查看/更换）")
        print("  配对码是按项目签发的：拿到哪个项目的码，就只能访问那个项目")
    if getattr(args, "reload", False):
        uvicorn.run(
            "godfather.app:get_app",
            factory=True,
            host=host,
            port=port,
            reload=True,
            log_level="info",
            # 关闭代理头重写：否则本机反代后面的 X-Forwarded-For 会被当作回环地址，
            # 配对凭证就能被绕过。
            proxy_headers=False,
        )
        return 0
    # 直接用当前 settings 建应用（避免工厂重新加载配置丢掉命令行修改）
    from .app import create_app

    uvicorn.run(
        create_app(settings),
        host=host,
        port=port,
        log_level="info",
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
