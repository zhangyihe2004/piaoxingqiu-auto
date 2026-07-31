from __future__ import annotations

import asyncio
import json
import unittest

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None

from piaoxingqiu_auto.platform.submission import (
    CART_CREATE_PATH,
    RiskEvent,
    SubmissionSession,
)


class FakeGuard:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, str | None]] = []

    def submitting(self) -> None:
        self.statuses.append(("SUBMITTING", None))

    def ready(self) -> None:
        self.statuses.append(("READY", None))

    def created(self, order_id: str | None = None) -> None:
        self.statuses.append(("CREATED", order_id))

    def unknown(self) -> None:
        self.statuses.append(("UNKNOWN", None))


@unittest.skipIf(async_playwright is None, "Playwright 未安装")
class SubmissionPlaywrightTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_resend_uses_one_real_route_lifecycle(self) -> None:
        payload = {"orders": [{"showId": "show", "quantity": 1}]}
        bodies: list[dict] = []
        server = await asyncio.start_server(
            lambda reader, writer: self._serve(reader, writer, bodies),
            "127.0.0.1",
            0,
        )
        port = server.sockets[0].getsockname()[1]

        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=True)
            except Exception as exc:
                server.close()
                await server.wait_closed()
                self.skipTest(f"Chromium 不可用：{exc}")
            page = await browser.new_page()
            await page.goto(f"http://127.0.0.1:{port}/")
            guard = FakeGuard()
            session = SubmissionSession(page, guard)
            risks: list[RiskEvent] = []
            await session.attach()

            async def send() -> None:
                await page.evaluate(
                    """
                    async ({ path, payload }) => {
                      const options = {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(payload)
                      };
                      await fetch(path, options);
                      await fetch(path, options);
                    }
                    """,
                    {"path": CART_CREATE_PATH, "payload": payload},
                )

            try:
                outcome = await session.submit(payload, send, 2, risks.append)
            finally:
                await session.detach()
                await browser.close()
                server.close()
                await server.wait_closed()

        self.assertTrue(outcome.result.success)
        self.assertEqual(outcome.request_count, 2)
        self.assertEqual(bodies, [payload, payload])
        self.assertEqual(
            risks,
            [RiskEvent("VERIFY_REQUIRED", "CREATE_ORDER", "verify-id", "show")],
        )
        self.assertEqual(
            guard.statuses,
            [("SUBMITTING", None), ("CREATED", "order-id")],
        )

    async def _serve(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        bodies: list[dict],
    ) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {
                key.lower(): value.strip()
                for line in lines[1:]
                if ":" in line
                for key, value in (line.split(":", 1),)
            }
            length = int(headers.get("content-length", "0"))
            body = await reader.readexactly(length) if length else b""
            extra_headers: dict[str, str] = {}
            if method == "POST" and path == CART_CREATE_PATH:
                bodies.append(json.loads(body))
                if len(bodies) == 1:
                    payload = {"statusCode": 33000000, "comments": "验证"}
                    extra_headers = {
                        "rc-code": "33007202",
                        "rc-id": "verify-id",
                        "rc-show-id": "show",
                    }
                else:
                    payload = {
                        "statusCode": 200,
                        "comments": "成功",
                        "data": {"orders": [{"orderId": "order-id"}]},
                    }
                content_type = "application/json"
                content = json.dumps(payload).encode()
            else:
                content_type = "text/html"
                content = b"<!doctype html><title>submission test</title>"
            response_headers = {
                "Content-Type": content_type,
                "Content-Length": str(len(content)),
                "Connection": "close",
                **extra_headers,
            }
            response = (
                "HTTP/1.1 200 OK\r\n"
                + "".join(f"{key}: {value}\r\n" for key, value in response_headers.items())
                + "\r\n"
            ).encode("latin-1")
            writer.write(response + content)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
