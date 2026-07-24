from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field


log = logging.getLogger("piaoxingqiu.auto")
LABELS = {
    "dynamic": "dynamic",
    "plan_inventory": "票档库存",
    "general_inventory": "票档库存",
    "seat_decode": "座位解码",
    "seat_score": "座位评分",
    "seat_page": "进入座位图",
    "seat_map": "选座页",
    "general_page": "购票页",
    "confirm_page": "确认页",
    "audience": "观演人",
    "submit_click": "提交点击",
    "create_response": "创建响应",
    "create_total": "创建总耗时",
}


@dataclass
class RunTimings:
    label: str
    attempt: int = 0
    started_at: float = 0.0
    values: dict[str, float] = field(default_factory=dict)

    def begin(self, attempt: int) -> None:
        self.attempt = attempt
        self.started_at = asyncio.get_running_loop().time()
        self.values.clear()

    def record(self, stage: str, seconds: float) -> None:
        if self.attempt:
            self.values[stage] = seconds

    def finish(self, outcome: str) -> None:
        if not self.attempt:
            return
        attempt = self.attempt
        elapsed = asyncio.get_running_loop().time() - self.started_at
        details = [
            f"{label}={self.values[name] * 1000:.0f}ms"
            for name, label in LABELS.items()
            if name in self.values
        ]
        details.append(f"关键路径总计={elapsed * 1000:.0f}ms")
        self.attempt = 0
        log.info(
            "抢票耗时｜%s｜尝试 %s｜结果 %s｜%s",
            self.label,
            attempt,
            outcome,
            "｜".join(details),
        )
