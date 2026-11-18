import asyncio
from collections import deque

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class LimitBody:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        messages: deque[Message] = deque()
        try:
            async with asyncio.timeout(5):
                await self.collect(receive, messages)
        except TimeoutError:
            await self.reject(scope, receive, send, 408, "REQUEST_TIMEOUT")
            return
        except OverflowError:
            await self.reject(scope, receive, send, 413, "REQUEST_TOO_LARGE")
            return
        if not messages:
            return

        async def replay() -> Message:
            return messages.popleft() if messages else await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def collect(receive: Receive, messages: deque[Message]) -> None:
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                messages.clear()
                return
            size += len(message.get("body", b""))
            if size > 32768:
                raise OverflowError
            if message.get("body") or not message.get("more_body"):
                messages.append(message)
            if not message.get("more_body"):
                break

    @staticmethod
    async def reject(
        scope: Scope, receive: Receive, send: Send, status: int, code: str
    ) -> None:
        response = JSONResponse(
            {
                "error": {
                    "code": code,
                    "message": code,
                    "request_id": scope.get("state", {}).get("request_id"),
                }
            },
            status_code=status,
        )
        await response(scope, receive, send)
