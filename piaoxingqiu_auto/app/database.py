from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from piaoxingqiu_auto.domain.models import (
    account_key,
    purchase_unit_qty,
    required_audience_count,
)


_UNCHANGED = object()


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    show_id TEXT NOT NULL,
    show_name TEXT NOT NULL,
    session_id TEXT NOT NULL,
    session_name TEXT NOT NULL,
    support_seat_picking INTEGER NOT NULL,
    show_limit INTEGER NOT NULL DEFAULT 0,
    session_limitation INTEGER NOT NULL DEFAULT 1,
    real_name_mode TEXT NOT NULL DEFAULT 'NONE',
    interval_sec INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    session_status TEXT NOT NULL DEFAULT '',
    sale_time_ms INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(show_id, session_id)
);

CREATE TABLE IF NOT EXISTS task_plans (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seat_plan_id TEXT NOT NULL,
    plan_name TEXT NOT NULL,
    price REAL NOT NULL,
    limitation INTEGER NOT NULL,
    unit_qty INTEGER NOT NULL DEFAULT 1,
    has_combo INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL,
    can_buy_count INTEGER NOT NULL DEFAULT 0,
    sale_started INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY(task_id, seat_plan_id),
    UNIQUE(task_id, priority)
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    phone TEXT NOT NULL UNIQUE,
    profile_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'RESERVED',
    last_error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bindings (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    quantity INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'STOPPED',
    order_id TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(task_id, account_id)
);

CREATE TABLE IF NOT EXISTS binding_audiences (
    task_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    masked_id TEXT NOT NULL,
    PRIMARY KEY(task_id, account_id, position),
    UNIQUE(task_id, account_id, name, masked_id),
    FOREIGN KEY(task_id, account_id)
        REFERENCES bindings(task_id, account_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS binding_plans (
    task_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    seat_plan_id TEXT NOT NULL,
    priority INTEGER NOT NULL,
    PRIMARY KEY(task_id, account_id, seat_plan_id),
    UNIQUE(task_id, account_id, priority),
    FOREIGN KEY(task_id, account_id)
        REFERENCES bindings(task_id, account_id) ON DELETE CASCADE
);
"""


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=15, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 15000")
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    @staticmethod
    def _next_id(connection: sqlite3.Connection, table: str) -> int:
        if table not in {"tasks", "accounts"}:
            raise ValueError(f"不支持为表 {table} 分配序号")
        row = connection.execute(
            f"""
            SELECT CASE
                WHEN EXISTS (SELECT 1 FROM {table} WHERE id = 1)
                THEN (
                    SELECT MIN(current.id + 1)
                    FROM {table} AS current
                    WHERE NOT EXISTS (
                        SELECT 1 FROM {table} AS following
                        WHERE following.id = current.id + 1
                    )
                )
                ELSE 1
            END AS id
            """
        ).fetchone()
        return int(row["id"])

    # ---- 抢票任务 ----

    def create_task(
        self,
        *,
        show_id: str,
        show_name: str,
        session_id: str,
        session_name: str,
        support_seat_picking: bool,
        show_limit: int,
        session_limitation: int,
        real_name_mode: str,
        interval_sec: int,
        session_status: str,
        sale_time_ms: int | None,
        plans: list[dict],
    ) -> tuple[int, bool]:
        now = time.time()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT id FROM tasks WHERE show_id = ? AND session_id = ?",
                (show_id, session_id),
            ).fetchone()
            if existing:
                self.connection.rollback()
                return int(existing["id"]), False
            task_id = self._next_id(self.connection, "tasks")
            self.connection.execute(
                """
                INSERT INTO tasks (
                    id, show_id, show_name, session_id, session_name,
                    support_seat_picking, show_limit, session_limitation,
                    real_name_mode, interval_sec, session_status, sale_time_ms,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    show_id,
                    show_name,
                    session_id,
                    session_name,
                    int(support_seat_picking),
                    show_limit,
                    session_limitation,
                    real_name_mode,
                    interval_sec,
                    session_status,
                    sale_time_ms,
                    now,
                    now,
                ),
            )
            self.connection.executemany(
                """
                INSERT INTO task_plans (
                    task_id, seat_plan_id, plan_name, price, limitation, priority,
                    unit_qty, has_combo, can_buy_count, sale_started, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        task_id,
                        plan["pid"],
                        plan["plan_name"],
                        plan["price"],
                        int(plan.get("limitation") or 0),
                        priority,
                        max(1, int(plan.get("unit_qty") or 1)),
                        int(bool(plan.get("has_combo"))),
                        int(plan.get("can_buy_count") or 0),
                        int(bool(plan.get("sale_started"))),
                        now,
                    )
                    for priority, plan in enumerate(plans, 1)
                ],
            )
            self.connection.commit()
            return task_id, True
        except Exception:
            self.connection.rollback()
            raise

    def list_tasks(self) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM tasks ORDER BY id").fetchall()

    def get_task(self, task_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()

    def get_task_plans(self, task_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM task_plans WHERE task_id = ? ORDER BY priority",
            (task_id,),
        ).fetchall()

    def update_task_snapshot(
        self,
        task_id: int,
        session_status: str,
        sale_time_ms: int | None,
        plans: list[tuple[str, int, bool]] | None,
    ) -> bool:
        now = time.time()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            changed = self.connection.execute(
                """
                UPDATE tasks
                SET session_status = ?,
                    sale_time_ms = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (session_status, sale_time_ms, now, task_id),
            ).rowcount
            if not changed:
                self.connection.rollback()
                return False
            if plans is not None:
                self.connection.execute(
                    """
                    UPDATE task_plans
                    SET can_buy_count = 0, sale_started = 0, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (now, task_id),
                )
                self.connection.executemany(
                    """
                    UPDATE task_plans
                    SET can_buy_count = ?, sale_started = ?, updated_at = ?
                    WHERE task_id = ? AND seat_plan_id = ?
                    """,
                    [
                        (count, int(started), now, task_id, plan_id)
                        for plan_id, count, started in plans
                    ],
                )
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def set_task_status(
        self, task_id: int, status: str, *, current_status: str | None = None
    ) -> bool:
        condition = " AND status = ?" if current_status is not None else ""
        parameters = [status, time.time(), task_id]
        if current_status is not None:
            parameters.append(current_status)
        return bool(
            self.connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?" + condition,
                parameters,
            ).rowcount
        )

    def set_task_interval(self, task_id: int, interval_sec: int) -> bool:
        return bool(
            self.connection.execute(
                "UPDATE tasks SET interval_sec = ?, updated_at = ? WHERE id = ?",
                (interval_sec, time.time(), task_id),
            ).rowcount
        )

    def delete_task(self, task_id: int) -> bool:
        with self.connection:
            changed = self.connection.execute(
                "DELETE FROM tasks WHERE id = ?", (task_id,)
            ).rowcount
        return bool(changed)

    # ---- 账号 ----

    def reserve_account(self, phone: str) -> sqlite3.Row:
        now = time.time()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT * FROM accounts WHERE phone = ?", (phone,)
            ).fetchone()
            if existing:
                raise ValueError(f"手机号已经是账号 #{existing['id']}")
            account_id = self._next_id(self.connection, "accounts")
            self.connection.execute(
                """
                INSERT INTO accounts (
                    id, phone, profile_key, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'RESERVED', ?, ?)
                """,
                (account_id, phone, account_key(phone), now, now),
            )
            account = self.get_account(account_id)
            if account is None:
                raise RuntimeError("账号创建后无法读取")
            self.connection.commit()
            return account
        except Exception:
            self.connection.rollback()
            raise

    def get_account(self, account_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()

    def get_account_by_phone(self, phone: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM accounts WHERE phone = ?", (phone,)
        ).fetchone()

    def list_accounts(self) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM accounts ORDER BY id").fetchall()

    def set_account_status(
        self,
        account_id: int,
        status: str,
        *,
        error: str = "",
    ) -> bool:
        with self.connection:
            changed = self.connection.execute(
                """
                UPDATE accounts
                SET status = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, error, time.time(), account_id),
            ).rowcount
            if not changed:
                return False
            if status == "NEEDS_LOGIN":
                self.connection.execute(
                    """
                    UPDATE bindings
                    SET status = 'NEEDS_LOGIN', updated_at = ?
                    WHERE account_id = ? AND status IN ('READY', 'RUNNING')
                    """,
                    (time.time(), account_id),
                )
            elif status == "READY":
                self.connection.execute(
                    """
                    UPDATE bindings
                    SET status = CASE WHEN enabled = 1 THEN 'READY' ELSE 'STOPPED' END,
                        updated_at = ?
                    WHERE account_id = ? AND status = 'NEEDS_LOGIN'
                    """,
                    (time.time(), account_id),
                )
            return True

    def delete_account(self, account_id: int) -> str | None:
        account = self.get_account(account_id)
        if not account:
            return None
        with self.connection:
            self.connection.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        return str(account["profile_key"])

    # ---- 任务账号绑定 ----

    def get_binding(self, task_id: int, account_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM bindings WHERE task_id = ? AND account_id = ?",
            (task_id, account_id),
        ).fetchone()

    def list_bindings(
        self, task_id: int | None = None, account_id: int | None = None
    ) -> list[sqlite3.Row]:
        conditions: list[str] = []
        parameters: list[int] = []
        if task_id is not None:
            conditions.append("task_id = ?")
            parameters.append(task_id)
        if account_id is not None:
            conditions.append("account_id = ?")
            parameters.append(account_id)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        return self.connection.execute(
            f"SELECT * FROM bindings{where} ORDER BY task_id, account_id",
            parameters,
        ).fetchall()

    def begin_binding_configuration(self, task_id: int, account_id: int) -> bool:
        binding = self.get_binding(task_id, account_id)
        if binding is None:
            return bool(self.get_task(task_id) and self.get_account(account_id))
        return bool(
            self.connection.execute(
                """
                UPDATE bindings
                SET enabled = 0,
                    status = CASE WHEN status = 'READY' THEN 'STOPPED' ELSE status END,
                    updated_at = ?
                WHERE task_id = ? AND account_id = ? AND status != 'RUNNING'
                """,
                (time.time(), task_id, account_id),
            ).rowcount
        )

    def activate_binding(self, task_id: int, account_id: int) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            account = self.get_account(account_id)
            if not account:
                raise ValueError(f"账号 #{account_id} 不存在")
            if account["status"] in {"RESERVED", "NEEDS_LOGIN"}:
                raise ValueError("账号尚未登录")
            task = self.get_task(task_id)
            binding = self.get_binding(task_id, account_id)
            if not task or not binding or not self.get_binding_plans(task_id, account_id):
                raise ValueError("绑定配置不完整")
            if int(binding["quantity"]) < 1:
                raise ValueError("目标数量已完成，请重新绑定并设置数量")
            people = self.get_binding_audiences(task_id, account_id)
            plans = self.get_binding_plans(task_id, account_id)
            ticket_count = int(binding["quantity"]) * purchase_unit_qty(
                plans,
                support_seat_picking=bool(task["support_seat_picking"]),
            )
            required = required_audience_count(
                task["real_name_mode"], ticket_count
            )
            if len(people) != required:
                raise ValueError("绑定观演人配置不完整")
            self.connection.execute(
                """
                UPDATE bindings
                SET enabled = 1, status = 'READY', last_error = '', updated_at = ?
                WHERE task_id = ? AND account_id = ?
                """,
                (time.time(), task_id, account_id),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def claim_binding(self, task_id: int, account_id: int) -> bool:
        """仅在绑定仍启用且空闲时原子取得一次执行权。"""
        return bool(
            self.connection.execute(
                """
                UPDATE bindings SET status = 'RUNNING', updated_at = ?
                WHERE task_id = ? AND account_id = ?
                  AND enabled = 1 AND status = 'READY'
                """,
                (time.time(), task_id, account_id),
            ).rowcount
        )

    def set_binding_status(
        self,
        task_id: int,
        account_id: int,
        status: str,
        *,
        order_id: str | None | object = _UNCHANGED,
        error: str = "",
    ) -> bool:
        keep_order_id = order_id is _UNCHANGED
        return bool(
            self.connection.execute(
                """
                UPDATE bindings
                SET status = ?,
                    order_id = CASE WHEN ? THEN order_id ELSE ? END,
                    last_error = ?,
                    updated_at = ?
                WHERE task_id = ? AND account_id = ?
                """,
                (
                    status,
                    int(keep_order_id),
                    None if keep_order_id else order_id,
                    error,
                    time.time(),
                    task_id,
                    account_id,
                ),
            ).rowcount
        )

    def deactivate_binding(self, task_id: int, account_id: int) -> bool:
        return bool(
            self.connection.execute(
                """
                UPDATE bindings
                SET enabled = 0,
                    status = CASE
                        WHEN status IN ('READY', 'RUNNING') THEN 'STOPPED'
                        ELSE status
                    END,
                    updated_at = ?
                WHERE task_id = ? AND account_id = ?
                """,
                (time.time(), task_id, account_id),
            ).rowcount
        )

    def get_binding_audiences(
        self, task_id: int, account_id: int
    ) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT * FROM binding_audiences
            WHERE task_id = ? AND account_id = ? ORDER BY position
            """,
            (task_id, account_id),
        ).fetchall()

    def save_binding_config(
        self,
        task_id: int,
        account_id: int,
        plan_ids: list[str],
        quantity: int,
        people: list[tuple[str, str]],
    ) -> None:
        """校验并以一个事务同时替换票档、观演人和运行状态。"""
        if not plan_ids or len(plan_ids) != len(set(plan_ids)):
            raise ValueError("至少选择一个不重复的票档")
        masked_ids = [masked_id for _, masked_id in people]
        if len(masked_ids) != len(set(masked_ids)):
            raise ValueError("同一证件不能重复添加")

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            account = self.get_account(account_id)
            task = self.get_task(task_id)
            if not account or not task:
                raise ValueError("任务或账号不存在")
            binding = self.get_binding(task_id, account_id)
            if binding and binding["status"] == "RUNNING":
                raise ValueError("绑定正在抢票，当前不能修改")
            known_plans = {
                row["seat_plan_id"]: row for row in self.get_task_plans(task_id)
            }
            if not set(plan_ids) <= known_plans.keys():
                raise ValueError("票档包含其他场次或已失效的编号")
            selected_plans = [known_plans[plan_id] for plan_id in plan_ids]
            unit_qty = purchase_unit_qty(
                selected_plans,
                support_seat_picking=bool(task["support_seat_picking"]),
            )
            maximum = int(task["session_limitation"]) // unit_qty
            if maximum < 1:
                raise ValueError("固定套票张数超过当前场次限购")
            if not 1 <= quantity <= maximum:
                raise ValueError(f"购票数量必须在 1~{maximum} 之间")
            required = required_audience_count(
                task["real_name_mode"], quantity * unit_qty
            )
            if len(people) != required:
                raise ValueError(f"当前实名规则需要配置 {required} 位观演人")
            if masked_ids:
                placeholders = ",".join("?" for _ in masked_ids)
                conflict = self.connection.execute(
                    f"""
                    SELECT account_id, masked_id
                    FROM binding_audiences
                    WHERE task_id = ? AND account_id != ?
                      AND masked_id IN ({placeholders})
                    LIMIT 1
                    """,
                    (task_id, account_id, *masked_ids),
                ).fetchone()
                if conflict:
                    raise ValueError(
                        f"证件 {conflict['masked_id']} 已配置在同场次账号 "
                        f"#{conflict['account_id']}"
                    )
            now = time.time()
            if binding:
                self.connection.execute(
                    """
                    UPDATE bindings
                    SET quantity = ?, enabled = 0, status = 'STOPPED',
                        order_id = NULL, last_error = '', updated_at = ?
                    WHERE task_id = ? AND account_id = ?
                    """,
                    (quantity, now, task_id, account_id),
                )
            else:
                self.connection.execute(
                    """
                    INSERT INTO bindings (
                        task_id, account_id, quantity, enabled, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 0, 'STOPPED', ?, ?)
                    """,
                    (task_id, account_id, quantity, now, now),
                )
            self.connection.execute(
                "DELETE FROM binding_plans WHERE task_id = ? AND account_id = ?",
                (task_id, account_id),
            )
            self.connection.executemany(
                """
                INSERT INTO binding_plans (
                    task_id, account_id, seat_plan_id, priority
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (task_id, account_id, plan_id, priority)
                    for priority, plan_id in enumerate(plan_ids, 1)
                ],
            )
            self.connection.execute(
                "DELETE FROM binding_audiences WHERE task_id = ? AND account_id = ?",
                (task_id, account_id),
            )
            self.connection.executemany(
                """
                INSERT INTO binding_audiences (
                    task_id, account_id, position, name, masked_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (task_id, account_id, position, name, masked_id)
                    for position, (name, masked_id) in enumerate(people, 1)
                ],
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def apply_fulfillment(
        self,
        task_id: int,
        account_id: int,
        quantity: int,
        masked_ids: tuple[str, ...],
    ) -> None:
        if quantity <= 0 and not masked_ids:
            return
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if masked_ids:
                placeholders = ",".join("?" for _ in masked_ids)
                self.connection.execute(
                    f"""
                    DELETE FROM binding_audiences
                    WHERE task_id = ? AND account_id = ?
                      AND masked_id IN ({placeholders})
                    """,
                    (task_id, account_id, *masked_ids),
                )
            self.connection.execute(
                """
                UPDATE bindings
                SET quantity = MAX(0, quantity - ?), updated_at = ?
                WHERE task_id = ? AND account_id = ?
                """,
                (max(0, quantity), time.time(), task_id, account_id),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def get_binding_plans(
        self, task_id: int, account_id: int
    ) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT p.*, bp.priority AS account_priority
            FROM binding_plans bp
            JOIN task_plans p
              ON p.task_id = bp.task_id
             AND p.seat_plan_id = bp.seat_plan_id
            WHERE bp.task_id = ? AND bp.account_id = ?
            ORDER BY bp.priority
            """,
            (task_id, account_id),
        ).fetchall()

    def delete_binding(self, task_id: int, account_id: int) -> bool:
        with self.connection:
            return bool(
                self.connection.execute(
                    "DELETE FROM bindings WHERE task_id = ? AND account_id = ?",
                    (task_id, account_id),
                ).rowcount
            )
