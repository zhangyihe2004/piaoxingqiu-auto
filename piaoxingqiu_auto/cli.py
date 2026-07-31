from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from . import __version__
from piaoxingqiu_auto.config import (
    CONFIG_PATH,
    DB_PATH,
    load_system_config,
)

if TYPE_CHECKING:
    from piaoxingqiu_auto.app.database import Database


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="piaoxingqiu-auto",
        description="票星球监控、飞书控制与自动创建订单",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="启动监控、飞书和多账号抢票服务")
    sub.add_parser("doctor", help="检查配置、数据库和 Playwright")
    sub.add_parser("test-feishu", help="发送一条飞书测试消息")
    args = parser.parse_args()
    try:
        if args.command == "serve":
            asyncio.run(_serve())
        elif args.command == "doctor":
            _doctor()
        else:
            asyncio.run(_test_feishu())
    except KeyboardInterrupt:
        print("\n已停止。", file=sys.stderr)
        raise SystemExit(130) from None
    except (
        ValueError,
        OSError,
        sqlite3.Error,
        httpx.HTTPError,
        RuntimeError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc


async def _serve() -> None:
    from piaoxingqiu_auto.adapters.feishu_gateway import FeishuGateway, IncomingCommand
    from piaoxingqiu_auto.app.commands import CommandWorker
    from piaoxingqiu_auto.app.database import Database
    from piaoxingqiu_auto.app.login_flow import FeishuLoginManager
    from piaoxingqiu_auto.app.scheduler import TaskScheduler
    from piaoxingqiu_auto.app.tasks import TaskService
    from piaoxingqiu_auto.platform.api import PxqClient
    from piaoxingqiu_auto.runtime.operator import OperatorGateway

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    system = load_system_config()
    db = Database(DB_PATH)
    _recover_bindings(db, system)
    client = PxqClient()
    queue: asyncio.Queue[IncomingCommand] = asyncio.Queue(maxsize=100)
    feishu = FeishuGateway(system.raw, queue)
    service = TaskService(db, client)
    operator = OperatorGateway()
    await operator.start()
    scheduler = TaskScheduler(db, service, feishu, system, operator)
    login = FeishuLoginManager(db, feishu, system, scheduler.cancel_account)
    worker = CommandWorker(queue, db, service, scheduler, login, feishu)
    tasks = [
        asyncio.create_task(scheduler.run_forever()),
        asyncio.create_task(feishu.run_forever()),
        asyncio.create_task(login.run_forever()),
        asyncio.create_task(worker.run_forever()),
    ]
    if not system.create_order_enabled:
        logging.getLogger("piaoxingqiu.auto").warning(
            "create_order_enabled=false：监控和登录正常，但不会创建订单"
        )
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await scheduler.close()
        await operator.close()
        await login.close()
        await feishu.aclose()
        await client.aclose()
        db.close()


def _doctor() -> None:
    from piaoxingqiu_auto.app.database import Database

    system = load_system_config()
    with Database(DB_PATH):
        pass
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ValueError(
            "Playwright 未安装，请执行 python -m pip install -e ."
        ) from exc
    try:
        with sync_playwright() as playwright:
            chromium = playwright.chromium.executable_path
            if not Path(chromium).is_file():
                raise ValueError(
                    "Chromium 未安装，请执行 python -m playwright install chromium"
                )
            browser = playwright.chromium.launch(
                headless=True,
                env={**os.environ, "CHROME_LOG_FILE": os.devnull},
            )
            browser.close()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Chromium 无法启动：{exc}") from exc
    print("环境检查")
    print(f"配置：{CONFIG_PATH}")
    print(f"数据库：{DB_PATH}")
    print(f"Chromium：{chromium}")
    print(f"无界面浏览器：{'是' if system.browser_headless else '否'}")
    print(f"账号并发：{system.max_concurrent_accounts}")
    print(f"创建订单：{'已启用' if system.create_order_enabled else '已禁用'}")
    print("检查：通过")


def _recover_bindings(db: Database, system) -> None:
    from piaoxingqiu_auto.app.run_config import build_login_config, build_order_config
    from piaoxingqiu_auto.platform.submission import PersistentOrderGuard

    for binding in db.list_bindings():
        account = db.get_account(binding["account_id"])
        task = db.get_task(binding["task_id"])
        if not account or not task:
            continue
        path = build_login_config(task, account, system).state_path
        try:
            state = PersistentOrderGuard.load(path)
        except (OSError, ValueError, KeyError, TypeError):
            db.set_binding_status(
                task["id"],
                account["id"],
                "UNKNOWN",
                error="订单保护文件损坏，请人工核对",
            )
            continue
        if state and state.status in {"SUBMITTING", "CREATED", "UNKNOWN"}:
            if binding["status"] not in {"CREATED", "UNKNOWN"}:
                try:
                    config = build_order_config(
                        task,
                        db.get_binding_plans(task["id"], account["id"]),
                        db.get_binding_stands(task["id"], account["id"]),
                        db.get_binding_audiences(task["id"], account["id"]),
                        account,
                        binding,
                        system,
                    )
                    state = PersistentOrderGuard(
                        config.state_path, config.plan_key
                    ).current()
                except (RuntimeError, ValueError):
                    db.set_binding_status(
                        task["id"],
                        account["id"],
                        "UNKNOWN",
                        error="订单保护文件属于旧配置，请人工核对",
                    )
                    continue
            protected_status = (
                "UNKNOWN" if state.status == "SUBMITTING" else state.status
            )
            db.set_binding_status(
                task["id"],
                account["id"],
                protected_status,
                order_id=state.order_id,
                error=(
                    "服务在创建订单时中断，请人工核对"
                    if protected_status == "UNKNOWN"
                    else ""
                ),
            )
        elif binding["status"] == "RUNNING":
            recovered_status = (
                "READY"
                if binding["enabled"] and account["status"] == "READY"
                else "NEEDS_LOGIN"
                if binding["enabled"]
                else "STOPPED"
            )
            db.set_binding_status(task["id"], account["id"], recovered_status)


async def _test_feishu() -> None:
    from piaoxingqiu_auto.adapters.feishu_gateway import FeishuError, FeishuGateway

    feishu = FeishuGateway(load_system_config().raw)
    try:
        if not await feishu.send_card(
            "piaoxingqiu-auto 测试", "飞书应用机器人通信正常。", "green"
        ):
            raise FeishuError("发送失败，请检查应用配置和默认群 chat_id")
    finally:
        await feishu.aclose()
    print("发送成功")
