from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SystemConfig:
    raw: dict
    browser_headless: bool
    browser_timeout_ms: int
    max_concurrent_accounts: int
    create_order_enabled: bool


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    booking_url: str
    support_seat_picking: bool


@dataclass(frozen=True)
class AudienceConfig:
    name: str
    masked_id: str


@dataclass(frozen=True)
class PurchaseConfig:
    session: str
    plans: tuple[str, ...]
    plan_ids: tuple[str, ...]
    quantity: int
    real_name_mode: str
    audiences: tuple[AudienceConfig, ...]


@dataclass(frozen=True)
class BrowserConfig:
    profile_dir: Path
    headless: bool
    timeout_ms: int


@dataclass(frozen=True)
class AccountRunConfig:
    project: ProjectConfig
    purchase: PurchaseConfig
    browser: BrowserConfig
    create_order: bool

    @property
    def state_path(self) -> Path:
        key = hashlib.sha256(self.project.booking_url.encode()).hexdigest()[:16]
        return self.browser.profile_dir.parent / "orders" / f"{key}.json"

    @property
    def plan_key(self) -> str:
        raw = "\n".join(
            (
                self.project.name,
                self.project.booking_url,
                self.purchase.session,
                *self.purchase.plans,
                *self.purchase.plan_ids,
                str(self.purchase.quantity),
                self.purchase.real_name_mode,
                *(f"{a.name}|{a.masked_id}" for a in self.purchase.audiences),
            )
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def required_audience_count(mode: str, quantity: int) -> int:
    if mode == "NONE":
        return 0
    if mode == "PER_ORDER":
        return 1
    return quantity


def account_key(phone: str) -> str:
    return hashlib.sha256(phone.encode()).hexdigest()[:20]
