"""创建订单状态保护与网络放行控制。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from playwright.async_api import Request, Route


GuardStatus = Literal["READY", "SUBMITTING", "CREATED", "UNKNOWN"]
GENERAL_CREATE_PATH = "/cyy_gatewayapi/trade/buyer/v1/items/orders/submit"
CART_CREATE_PATH = "/cyy_gatewayapi/trade/buyer/order/cart/v1/create_order"
CREATE_PATHS = frozenset({GENERAL_CREATE_PATH, CART_CREATE_PATH})


def is_create_url(url: str) -> bool:
    return urlsplit(url).path.lower() in CREATE_PATHS


@dataclass
class OrderState:
    plan_key: str
    status: GuardStatus
    updated_at: str
    order_id: str | None = None


class PersistentOrderGuard:
    def __init__(self, path: Path, plan_key: str) -> None:
        self.path = path
        self.plan_key = plan_key

    def current(self) -> OrderState:
        try:
            state = self.load(self.path)
            if state is None:
                return self._new("READY")
            if state.plan_key != self.plan_key:
                if state.status != "READY":
                    raise RuntimeError(
                        "状态文件属于其他绑定且仍有订单保护，请先人工核对"
                    )
                return self._new("READY")
            return state
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"下单状态文件损坏，请人工检查：{self.path}") from exc

    @staticmethod
    def load(path: Path) -> OrderState | None:
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        status = raw["status"]
        if status not in {"READY", "SUBMITTING", "CREATED", "UNKNOWN"}:
            raise ValueError(f"未知状态：{status}")
        return OrderState(
            plan_key=str(raw["plan_key"]),
            status=status,
            updated_at=str(raw["updated_at"]),
            order_id=raw.get("order_id"),
        )

    def require_ready(self) -> None:
        state = self.current()
        if state.status != "READY":
            raise RuntimeError(
                f"当前计划状态为 {state.status}，为避免重复订单已禁止再次提交。"
                f"请先在票星球“待支付”订单中人工核对。状态文件：{self.path}"
            )

    def submitting(self) -> None:
        self._write(self._new("SUBMITTING"))

    def ready(self) -> None:
        self._write(self._new("READY"))

    def created(self, order_id: str | None = None) -> None:
        self._write(self._new("CREATED", order_id=order_id))

    def unknown(self) -> None:
        self._write(self._new("UNKNOWN"))

    @staticmethod
    def clear(path: Path) -> None:
        path.unlink(missing_ok=True)

    def _new(
        self,
        status: GuardStatus,
        *,
        order_id: str | None = None,
    ) -> OrderState:
        return OrderState(
            plan_key=self.plan_key,
            status=status,
            updated_at=datetime.now(timezone.utc).isoformat(),
            order_id=order_id,
        )

    def _write(self, state: OrderState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(state), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


class OrderFirewall:
    """创建订单请求默认拒绝；武装后仅放行一个请求。"""

    def __init__(self) -> None:
        self.armed = False
        self.attempt_allowed = False
        self.blocked_requests = 0
        self.unexpected_posts: set[str] = set()

    async def route(self, route: Route, request: Request) -> None:
        if request.method.upper() != "POST":
            await route.continue_()
            return

        if is_create_url(request.url):
            if self.armed and not self.attempt_allowed:
                self.attempt_allowed = True
                await route.continue_()
                return
            self.blocked_requests += 1
            await route.abort("blockedbyclient")
            return

        if self.armed:
            self.unexpected_posts.add(request.url.partition("?")[0])
            self.blocked_requests += 1
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    def arm_once(self) -> None:
        if self.armed:
            raise RuntimeError("创建请求防火墙已处于武装状态")
        self.armed = True
        self.attempt_allowed = False

    def disarm(self) -> None:
        self.armed = False
