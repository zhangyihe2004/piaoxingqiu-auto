from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from playwright.async_api import Request

if TYPE_CHECKING:
    from piaoxingqiu_auto.platform.booking import PurchasePage

class AuthenticationError(RuntimeError):
    pass


class AuthenticationRequired(AuthenticationError):
    pass


@dataclass(frozen=True)
class OfficialAudience:
    id: str
    name: str
    masked_id: str


class AuthGuard:
    def __init__(
        self,
        site: PurchasePage,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.site = site
        self.headers = headers if headers is not None else {}
        self._verified_at = 0.0
        self._validation_suspended = False
        root = f"{site.origin}/cyy_gatewayapi/show"
        self.endpoint = f"{root}/buyer/v5/show/{site.booking_ids[0]}/show_user"

    def suspend_validation(self) -> None:
        self._validation_suspended = True

    async def ensure(self) -> None:
        if self._validation_suspended:
            if not self.headers:
                await self._capture()
            return
        if self.headers and await self.check():
            return
        await self.refresh()

    async def refresh(self) -> None:
        await self._capture()
        if not await self.check():
            raise AuthenticationRequired("登录状态无效，请通过飞书重新登录")

    async def _capture(self) -> None:
        try:
            headers = await capture_authenticated_headers(self.site)
        except AuthenticationRequired:
            raise AuthenticationRequired("登录状态无效，请通过飞书重新登录") from None
        self.headers.clear()
        self.headers.update(headers)

    async def require_recent(self, max_age: float = 5.0) -> None:
        if self._validation_suspended:
            return
        if asyncio.get_running_loop().time() - self._verified_at <= max_age:
            return
        if not await self.check():
            raise AuthenticationRequired("登录状态失效，请通过飞书重新登录")

    async def check(self) -> bool:
        if not self.headers:
            return False
        query = urlencode(request_context(self.headers))
        response = await self.site.page.context.request.get(
            f"{self.endpoint}?{query}", headers=self.headers
        )
        if response.status in {401, 403}:
            self._verified_at = 0.0
            return False
        if response.status in {429, 469}:
            raise AuthenticationError(
                f"登录检查触发限制（HTTP {response.status}），已停止"
            )
        if not response.ok:
            raise AuthenticationError(f"登录检查返回 HTTP {response.status}")
        payload = await response.json()
        if not isinstance(payload, dict):
            raise AuthenticationError("登录检查响应不是 JSON 对象")
        if str(payload.get("statusCode")) != "200":
            self._verified_at = 0.0
            return False
        self._verified_at = asyncio.get_running_loop().time()
        return True

    async def audiences(self) -> tuple[OfficialAudience, ...]:
        await self.ensure()
        query = urlencode(request_context(self.headers))
        response = await self.site.page.context.request.get(
            f"{self.site.origin}/cyy_gatewayapi/user/buyer/v3/user_audiences?{query}",
            headers=self.headers,
        )
        if not response.ok:
            raise AuthenticationError(f"读取官方观演人返回 HTTP {response.status}")
        payload = await response.json()
        if not isinstance(payload, dict) or str(payload.get("statusCode")) != "200":
            raise AuthenticationError("读取官方观演人失败")
        data = payload.get("data")
        if not isinstance(data, list):
            raise AuthenticationError("官方观演人响应格式错误")
        result: list[OfficialAudience] = []
        for item in data:
            if (
                not isinstance(item, dict)
                or item.get("isValid") is False
                or item.get("selectable") is False
            ):
                continue
            audience_id = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            masked_id = _mask_document(str(item.get("idNo") or ""))
            if audience_id and name and masked_id:
                result.append(OfficialAudience(audience_id, name, masked_id))
        return tuple(result)

def request_context(headers: dict[str, str] | None = None) -> dict[str, str]:
    headers = headers or {}
    return {
        "lang": "zh",
        "utcOffset": headers.get("utc-offset", "480"),
        "terminalSrc": headers.get("terminal-src", "H5"),
        "ver": headers.get("ver", "4.63.3"),
        "currency": "CNY",
    }


async def capture_authenticated_headers(site: PurchasePage) -> dict[str, str]:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Request] = loop.create_future()

    def capture(request: Request) -> None:
        if (
            not future.done()
            and request.method.upper() == "GET"
            and "/buyer/" in request.url
            and request.headers.get("access-token")
        ):
            future.set_result(request)

    site.page.context.on("request", capture)
    try:
        await site.open_purchase()
        try:
            request = await asyncio.wait_for(
                future,
                timeout=site.config.browser.timeout_ms / 1000,
            )
        except TimeoutError as exc:
            raise AuthenticationRequired("booking 未产生有效的认证请求") from exc
    finally:
        site.page.context.remove_listener("request", capture)
    return {
        name: value
        for name, value in request.headers.items()
        if not name.startswith(":") and name.lower() not in {"host", "content-length"}
    }


def _mask_document(value: str) -> str:
    value = re.sub(r"\s+", "", value)
    if len(value) < 7:
        return ""
    return f"{value[:3]}{'*' * (len(value) - 7)}{value[-4:]}"
