"""SSE 进度事件流。

事件写入 events 表，这里按 id 游标增量读取：
- 跨进程安全（独立 worker 也能推事件）；
- 断线可用 Last-Event-ID 续传。
"""
from __future__ import annotations

import asyncio
import threading

from fastapi import APIRouter, Depends, Request
from starlette.background import BackgroundTask
from fastapi.responses import JSONResponse, StreamingResponse

from ..context import AppContext
from ..events import format_sse, read_events
from .deps import get_ctx

router = APIRouter(tags=["events"])

# 单次请求最多取多少条（防止一个请求拉走整张表）
MAX_LIMIT = 1000


@router.get("/api/events/history")
def events_history(
    request: Request,
    project_id: str = "",
    after: int = 0,
    limit: int = 200,
    all_projects: bool = False,
    ctx: AppContext = Depends(get_ctx),
) -> list[dict]:
    # limit 必须有上限，否则一个请求就能把整张事件表拉出来
    limit = max(1, min(int(limit), MAX_LIMIT))
    # scope 语义：具体项目 / None（只全局事件）/ ""（全部项目）
    # 局域网会话只解锁一个项目：事件不能跨项目。
    session_project = getattr(request.state, "pairing_project", None)
    scope = session_project or project_id or ("" if all_projects else None)
    return read_events(ctx.db, after_id=after, project_id=scope, limit=limit)


# 同时打开的 SSE 连接数上限：每条连接占用一个线程 + 一个 SQLite 连接
MAX_SSE_CONNECTIONS = 32
_sse_connections = 0
_sse_lock = threading.Lock()


@router.get("/api/events")
async def stream_events(
    request: Request, project_id: str = "", after: int = 0, all_projects: bool = False
) -> StreamingResponse:
    """事件流（SSE）。

    - 连接数有上限：每条连接占用一个工作线程与一个 SQLite 连接；
    - 不带 project_id 时只订阅全局事件，跨项目订阅要显式 all_projects=1；
    - Last-Event-ID 优先于 after（浏览器重连时用它续传）。
    """
    ctx: AppContext = request.app.state.ctx
    global _sse_connections
    with _sse_lock:
        if _sse_connections >= MAX_SSE_CONNECTIONS:
            return JSONResponse(
                status_code=503,
                content={"detail": "事件订阅连接数已达上限，请稍后再试", "code": "too_many_streams"},
            )
        _sse_connections += 1

    last_id = after
    header_last = request.headers.get("last-event-id")
    if header_last and header_last.isdigit():
        last_id = int(header_last)

    session_project = getattr(request.state, "pairing_project", None)
    scope = session_project or project_id or ("" if all_projects else None)

    async def generator():
        nonlocal last_id
        try:
            yield "retry: 2000\n\n"
            idle = 0
            while True:
                if await request.is_disconnected():
                    break
                events = await asyncio.to_thread(
                    read_events, ctx.db, after_id=last_id, project_id=scope, limit=100
                )
                if events:
                    idle = 0
                    for event in events:
                        last_id = event["id"]
                        yield format_sse(event)
                else:
                    idle += 1
                    # 心跳，避免代理层断开长连接
                    if idle % 20 == 0:
                        yield ": keep-alive\n\n"
                await asyncio.sleep(0.5)
        finally:
            _release_sse_connection()

    # 生成器可能一次都没被启动（客户端在响应头后就断开），
    # 这时生成器里的 finally 不会执行 → 连接计数只增不减，
    # 32 次异常断连之后 SSE 就永久 503 了。
    # BackgroundTask 在响应结束后一定会执行，用它兜底释放。
    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
        background=BackgroundTask(_release_sse_connection),
    )


def _release_sse_connection() -> None:
    global _sse_connections
    with _sse_lock:
        _sse_connections = max(0, _sse_connections - 1)


