from dataclasses import dataclass


@dataclass(frozen=True)
class RunResult:
    status: str
    message: str
    order_id: str | None = None
    removed_audiences: tuple[str, ...] = ()
    fulfilled_quantity: int = 0
    reuse_page: bool = False
