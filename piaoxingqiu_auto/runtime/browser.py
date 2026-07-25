from __future__ import annotations

import os
import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
import time

from playwright.async_api import (
    BrowserContext,
    Error,
    Page,
    Playwright,
    async_playwright,
)

from piaoxingqiu_auto.domain.models import BrowserConfig


MAX_WARM_PAGES_PER_ACCOUNT = 2


@dataclass
class WarmTaskPage:
    page: Page
    auth_headers: dict[str, str] = field(default_factory=dict)
    last_used: float = field(default_factory=time.monotonic)


async def blank_page(context: BrowserContext, claimed: set[Page] | None = None) -> Page:
    claimed = claimed or set()
    return (
        next(
            (
                page
                for page in context.pages
                if page not in claimed
                and not page.is_closed()
                and page.url == "about:blank"
            ),
            None,
        )
        or await context.new_page()
    )


@asynccontextmanager
async def persistent_browser(
    config: BrowserConfig,
) -> AsyncIterator[BrowserContext]:
    config.profile_dir.mkdir(parents=True, exist_ok=True)
    playwright: Playwright = await async_playwright().start()
    context: BrowserContext | None = None
    try:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(config.profile_dir),
            headless=config.headless,
            viewport={"width": 430, "height": 900},
            screen={"width": 430, "height": 900},
            device_scale_factor=1,
            has_touch=True,
            is_mobile=True,
            user_agent=(
                "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/149.0.0.0 Mobile Safari/537.36"
            ),
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            env={**os.environ, "CHROME_LOG_FILE": os.devnull},
        )
        context.set_default_timeout(config.timeout_ms)
        yield context
    except Exception as exc:
        message = str(exc)
        if (
            "ProcessSingleton" in message
            or "user data directory is already in use" in message
        ):
            raise RuntimeError(
                "浏览器资料目录正在使用，请先关闭该账号的其他浏览器实例"
            ) from exc
        raise
    finally:
        if context is not None:
            with suppress(Error):
                await context.close()
        with suppress(Error):
            await playwright.stop()


class AccountBrowserPool:
    """每个账号保留一个浏览器；同一账号的页面操作严格串行。"""

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._managers: dict[int, AbstractAsyncContextManager[BrowserContext]] = {}
        self._contexts: dict[int, BrowserContext] = {}
        self._pages: dict[tuple[int, int], WarmTaskPage] = {}
        self._cache: dict[int, dict[str, object]] = {}

    def account_cache(self, account_id: int) -> dict[str, object]:
        return self._cache.setdefault(account_id, {})

    @asynccontextmanager
    async def use(
        self, account_id: int, config: BrowserConfig
    ) -> AsyncIterator[BrowserContext]:
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            context = self._contexts.get(account_id)
            if context is None:
                manager = persistent_browser(config)
                context = await manager.__aenter__()
                self._managers[account_id] = manager
                self._contexts[account_id] = context
            try:
                yield context
            except Error as exc:
                if "closed" in str(exc).lower():
                    closed_manager = self._managers.pop(account_id, None)
                    self._contexts.pop(account_id, None)
                    self._forget_pages(account_id)
                    if closed_manager is not None:
                        await closed_manager.__aexit__(None, None, None)
                raise

    async def task_page(
        self,
        account_id: int,
        task_id: int,
        context: BrowserContext,
    ) -> WarmTaskPage:
        for page_key, item in tuple(self._pages.items()):
            if page_key[0] == account_id and item.page.is_closed():
                self._pages.pop(page_key, None)
                await self._close_page(item)
        key = (account_id, task_id)
        warm = self._pages.get(key)
        if warm is not None:
            warm.last_used = time.monotonic()
            return warm

        account_pages = [
            (page_key, item)
            for page_key, item in self._pages.items()
            if page_key[0] == account_id
        ]
        if len(account_pages) >= MAX_WARM_PAGES_PER_ACCOUNT:
            old_key, old = min(account_pages, key=lambda pair: pair[1].last_used)
            self._pages.pop(old_key, None)
            await self._close_page(old)

        claimed = {
            item.page for item in self._pages.values() if not item.page.is_closed()
        }
        page = await blank_page(context, claimed)
        warm = WarmTaskPage(page)
        self._pages[key] = warm
        return warm

    async def close_task_page(self, account_id: int, task_id: int) -> None:
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            warm = self._pages.pop((account_id, task_id), None)
            if warm is not None:
                await self._close_page(warm)

    async def close(self, account_id: int) -> None:
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            manager = self._managers.pop(account_id, None)
            self._contexts.pop(account_id, None)
            self._forget_pages(account_id)
            if manager is not None:
                await manager.__aexit__(None, None, None)

    async def close_all(self) -> None:
        for account_id in tuple(self._managers):
            await self.close(account_id)

    def _forget_pages(self, account_id: int) -> None:
        self._cache.pop(account_id, None)
        for key in tuple(self._pages):
            if key[0] == account_id:
                self._pages.pop(key, None)

    @staticmethod
    async def _close_page(warm: WarmTaskPage) -> None:
        if not warm.page.is_closed():
            with suppress(Error):
                await warm.page.close()


async def save_screenshot(page, directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.png"
    await page.screenshot(path=str(path), full_page=True)
    return path
