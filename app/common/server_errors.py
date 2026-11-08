"""
Unhandled exceptions become a JSON 500 *inside* the CORS middleware.

Starlette answers an unhandled exception from ServerErrorMiddleware, which is
the outermost layer, so that 500 never passes through CORSMiddleware and
carries no Access-Control-Allow-Origin header. The browser then hides the
response and the frontend sees only "Failed to fetch" (a conversation delete
hitting a foreign key showed exactly that). Installed before CORS in main.py,
this layer sits inside it, so the 500 gets CORS headers and its body.

Plain ASGI rather than BaseHTTPMiddleware, which would buffer the agent's SSE
stream. An exception after the response has started is re-raised: the status
line is already sent and there is nothing honest left to replace it with.
"""
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logger import get_logger

logger = get_logger(__name__)


class JsonServerErrors:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            if started:
                raise
            logger.exception("Unhandled error on %s %s", scope.get("method"), scope.get("path"))
            await JSONResponse({"detail": "Internal server error."}, status_code=500)(
                scope, receive, send
            )
