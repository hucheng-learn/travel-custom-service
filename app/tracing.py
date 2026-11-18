import logging
from time import perf_counter
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


logger = logging.getLogger("uvicorn.error")


class TraceMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        trace_id = uuid4().hex
        scope.setdefault("state", {})["trace_id"] = trace_id
        started = perf_counter()
        status = 500

        async def traced_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Trace-Id"] = trace_id
            await send(message)

        try:
            await self.app(scope, receive, traced_send)
        finally:
            if scope["path"].startswith("/api/v1/support"):
                # 记路由模板和耗时，不输出用户输入、中台正文、密钥或数据库 URL。
                route = getattr(scope.get("route"), "path", "<unmatched>")
                logger.info("support_http trace_id=%s method=%s route=%s status=%s ms=%.0f",
                            trace_id, scope["method"], route, status, (perf_counter() - started) * 1000)
