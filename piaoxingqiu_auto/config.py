from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from piaoxingqiu_auto.domain.models import SystemConfig


BASE_DIR = Path(
    os.environ.get("PIAOXINGQIU_AUTO_DIR", Path.home() / ".piaoxingqiu-auto")
)
CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "piaoxingqiu-auto.db"
ACCOUNTS_DIR = BASE_DIR / "accounts"

DEFAULT_CONFIG = {
    "feishu_app_id": "",
    "feishu_app_secret": "",
    "feishu_admin_open_ids": [],
    "feishu_default_chat_id": "",
    "browser_headless": True,
    "browser_timeout_seconds": 10,
    "max_concurrent_accounts": 4,
    "create_order_enabled": False,
}


def load_system_config() -> SystemConfig:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if os.name == "posix":
        CONFIG_PATH.chmod(0o600)
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config.json 根节点必须是对象")
    expected = set(DEFAULT_CONFIG)
    missing = expected - set(raw)
    unknown = set(raw) - expected
    if missing:
        raise ValueError(f"config.json 缺少字段：{', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"config.json 包含未知字段：{', '.join(sorted(unknown))}")
    for key in ("feishu_app_id", "feishu_app_secret", "feishu_default_chat_id"):
        if not isinstance(raw[key], str):
            raise ValueError(f"config.json 字段 {key} 必须是字符串")
    admins = raw["feishu_admin_open_ids"]
    if not isinstance(admins, list) or any(
        not isinstance(item, str) for item in admins
    ):
        raise ValueError("feishu_admin_open_ids 必须是字符串数组")
    for key in ("browser_headless", "create_order_enabled"):
        if not isinstance(raw[key], bool):
            raise ValueError(f"config.json 字段 {key} 必须是 true 或 false")
    timeout = raw["browser_timeout_seconds"]
    concurrent = raw["max_concurrent_accounts"]
    if type(timeout) is not int or not 5 <= timeout <= 120:
        raise ValueError("browser_timeout_seconds 必须在 5~120 之间")
    if type(concurrent) is not int or not 1 <= concurrent <= 20:
        raise ValueError("max_concurrent_accounts 必须在 1~20 之间")
    return SystemConfig(
        raw=raw,
        browser_headless=raw["browser_headless"],
        browser_timeout_ms=timeout * 1000,
        max_concurrent_accounts=concurrent,
        create_order_enabled=raw["create_order_enabled"],
    )


def account_home(profile_key: str) -> Path:
    return ACCOUNTS_DIR / profile_key


def remove_account_home(key: str) -> None:
    home = account_home(key).resolve()
    root = ACCOUNTS_DIR.resolve()
    if home.parent != root:
        raise RuntimeError("拒绝删除账号目录：路径越界")
    if home.exists():
        shutil.rmtree(home)


def validate_phone(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"1\d{10}", value):
        raise ValueError("请输入 11 位大陆手机号")
    return value


def mask_phone(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}"
