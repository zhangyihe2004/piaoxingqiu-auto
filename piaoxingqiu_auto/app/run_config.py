from __future__ import annotations

from piaoxingqiu_auto.config import account_home
from piaoxingqiu_auto.domain.models import (
    AccountRunConfig,
    AudienceConfig,
    BrowserConfig,
    ProjectConfig,
    PurchaseConfig,
    SystemConfig,
    required_audience_count,
)


def build_order_config(
    task, plans, audiences, account, binding, system: SystemConfig
) -> AccountRunConfig:
    if not plans:
        raise RuntimeError("绑定尚未配置票档")
    people = tuple(
        AudienceConfig(person["name"], person["masked_id"]) for person in audiences
    )
    quantity = int(binding["quantity"])
    if quantity < 1 or len(people) != required_audience_count(
        task["real_name_mode"], quantity
    ):
        raise RuntimeError("绑定数量或观演人配置不完整")
    return AccountRunConfig(
        project=_project_config(task),
        purchase=PurchaseConfig(
            task["session_name"],
            tuple(plan["plan_name"] for plan in plans),
            tuple(plan["seat_plan_id"] for plan in plans),
            quantity,
            task["real_name_mode"],
            people,
        ),
        browser=_browser_config(account, system),
        create_order=system.create_order_enabled,
    )


def build_login_config(task, account, system: SystemConfig) -> AccountRunConfig:
    return AccountRunConfig(
        project=_project_config(task),
        purchase=PurchaseConfig(
            task["session_name"], (), (), 0, task["real_name_mode"], ()
        ),
        browser=_browser_config(account, system),
        create_order=False,
    )


def _browser_config(account, system: SystemConfig) -> BrowserConfig:
    return BrowserConfig(
        account_home(account["profile_key"]) / "browser-profile",
        system.browser_headless,
        system.browser_timeout_ms,
    )


def _project_config(task) -> ProjectConfig:
    show_id = task["show_id"]
    session_id = task["session_id"]
    support_seat_picking = bool(task["support_seat_picking"])
    seat_pick_type = "SUPPORT_SEAT" if support_seat_picking else "SUPPORT_NONE"
    return ProjectConfig(
        task["show_name"],
        f"https://m.piaoxingqiu.com/booking/{show_id}"
        f"?saleShowSessionId={session_id}&seatPickType={seat_pick_type}&showId={show_id}",
        support_seat_picking,
    )
