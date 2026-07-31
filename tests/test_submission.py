from __future__ import annotations

import unittest
from unittest.mock import patch

from piaoxingqiu_auto.platform.submission import (
    CART_CREATE_PATH,
    CreateResult,
    SubmissionLease,
    SubmissionSession,
    create_failure_action,
    parse_create_response,
)


class FakeRequest:
    method = "POST"
    url = f"https://m.piaoxingqiu.com{CART_CREATE_PATH}"

    def __init__(self, payload: dict) -> None:
        self.post_data_json = payload


class FakeResponse:
    url = f"https://m.piaoxingqiu.com{CART_CREATE_PATH}"
    status = 200
    ok = True

    def __init__(
        self,
        payload: dict,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.request = FakeRequest({})
        self.headers = headers or {}
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


class FakeGuard:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, str | None]] = []

    def submitting(self) -> None:
        self.statuses.append(("SUBMITTING", None))

    def ready(self) -> None:
        self.statuses.append(("READY", None))

    def created(self, order_id: str | None = None) -> None:
        self.statuses.append(("CREATED", order_id))

    def unknown(self) -> None:
        self.statuses.append(("UNKNOWN", None))


class SubmissionLeaseTests(unittest.TestCase):
    def test_only_identical_payload_can_retry(self) -> None:
        lease = SubmissionLease()
        payload = {"orders": [{"id": "same"}]}
        lease.open(payload)

        self.assertTrue(lease.authorize(FakeRequest(payload)))
        self.assertFalse(lease.authorize(FakeRequest(payload)))

        lease.response_received()
        self.assertFalse(lease.authorize(FakeRequest(payload)))

        lease.allow_retry()
        self.assertFalse(lease.authorize(FakeRequest({"orders": [{"id": "other"}]})))
        self.assertTrue(lease.authorize(FakeRequest(payload)))
        self.assertEqual(lease.request_count, 2)


class SubmissionSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_limiting_retries_same_logical_order(self) -> None:
        payload = {"orders": [{"id": "same"}]}
        guard = FakeGuard()
        session = SubmissionSession(None, guard)
        calls = 0

        async def send() -> None:
            nonlocal calls
            calls += 1
            self.assertTrue(session.lease.authorize(FakeRequest(payload)))
            response = (
                FakeResponse({"statusCode": 33000000, "mode": "limiting"})
                if calls == 1
                else FakeResponse(
                    {
                        "statusCode": 200,
                        "comments": "成功",
                        "data": {"orders": [{"orderId": "order-id"}]},
                    }
                )
            )
            session._handle_response(response)

        with patch(
            "piaoxingqiu_auto.platform.submission.LIMITING_INTERVAL_SECONDS", 0
        ):
            outcome = await session.submit(payload, send, 1)

        self.assertTrue(outcome.result.success)
        self.assertEqual(outcome.request_count, 2)
        self.assertEqual(calls, 2)
        self.assertEqual(
            guard.statuses,
            [("SUBMITTING", None), ("CREATED", "order-id")],
        )

    async def test_limiting_stops_after_three_retries(self) -> None:
        payload = {"orders": [{"id": "same"}]}
        guard = FakeGuard()
        session = SubmissionSession(None, guard)
        calls = 0

        async def send() -> None:
            nonlocal calls
            calls += 1
            self.assertTrue(session.lease.authorize(FakeRequest(payload)))
            session._handle_response(
                FakeResponse({"statusCode": 33000000, "mode": "limiting"})
            )

        with patch(
            "piaoxingqiu_auto.platform.submission.LIMITING_INTERVAL_SECONDS", 0
        ):
            outcome = await session.submit(payload, send, 1)

        self.assertFalse(outcome.result.success)
        self.assertEqual(outcome.request_count, 4)
        self.assertEqual(calls, 4)
        self.assertEqual(
            guard.statuses,
            [("SUBMITTING", None), ("READY", None)],
        )

    async def test_captcha_allows_official_resend(self) -> None:
        payload = {"orders": [{"id": "same"}]}
        guard = FakeGuard()
        session = SubmissionSession(None, guard)

        async def send() -> None:
            self.assertTrue(session.lease.authorize(FakeRequest(payload)))
            session._handle_response(
                FakeResponse(
                    {"statusCode": 33000000},
                    headers={"rc-code": "33007202"},
                )
            )
            self.assertTrue(session.lease.authorize(FakeRequest(payload)))
            session._handle_response(
                FakeResponse(
                    {
                        "statusCode": 200,
                        "comments": "成功",
                        "data": {"orders": [{"orderId": "order-id"}]},
                    }
                )
            )

        outcome = await session.submit(payload, send, 1)

        self.assertTrue(outcome.result.success)
        self.assertEqual(outcome.request_count, 2)
        self.assertEqual(
            guard.statuses,
            [("SUBMITTING", None), ("CREATED", "order-id")],
        )

    async def test_sender_failure_before_request_restores_ready(self) -> None:
        guard = FakeGuard()
        session = SubmissionSession(None, guard)

        async def send() -> None:
            raise RuntimeError("client unavailable")

        with self.assertRaisesRegex(RuntimeError, "client unavailable"):
            await session.submit({"orders": []}, send, 1)

        self.assertEqual(
            guard.statuses,
            [("SUBMITTING", None), ("READY", None)],
        )


class ResponseParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_header_has_priority(self) -> None:
        result = await parse_create_response(
            FakeResponse(
                {"statusCode": 33000000, "comments": "排队"},
                headers={
                    "rc-code": "33007201",
                    "rc-id": "queue-id",
                    "rc-show-id": "show-id",
                },
            )
        )

        self.assertEqual(result.risk_state, "QUEUEING")
        self.assertEqual(result.rc_id, "queue-id")
        self.assertEqual(result.rc_show_id, "show-id")

    async def test_captcha_header_is_not_login_failure(self) -> None:
        result = await parse_create_response(
            FakeResponse(
                {"statusCode": 33000000},
                headers={"rc-code": "33007202"},
            )
        )

        self.assertEqual(result.risk_state, "VERIFY_REQUIRED")

    async def test_limiting_mode_is_separate_from_business_code(self) -> None:
        result = await parse_create_response(
            FakeResponse(
                {
                    "statusCode": 33000000,
                    "mode": "limiting",
                    "comments": "正在为您自动尝试",
                }
            )
        )

        self.assertEqual(result.risk_state, "RATE_LIMITED")

    async def test_success_details_are_extracted(self) -> None:
        result = await parse_create_response(
            FakeResponse(
                {
                    "statusCode": 200,
                    "comments": "成功",
                    "data": {
                        "paidDeadLineTime": 1780000000000,
                        "unPaidTransactionIds": ["transaction"],
                        "orders": [
                            {
                                "orderId": "order-id",
                                "orderNumber": "order-number",
                            }
                        ],
                    },
                }
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(result.order_id, "order-id")
        self.assertEqual(result.order_number, "order-number")
        self.assertEqual(result.payment_deadline_ms, 1780000000000)
        self.assertEqual(result.unpaid_transaction_count, 1)


class FailurePolicyTests(unittest.TestCase):
    def test_33000000_is_not_blindly_reselected(self) -> None:
        result = CreateResult(
            False,
            None,
            None,
            None,
            0,
            200,
            "33000000",
            None,
            "正在为您自动尝试",
        )

        self.assertEqual(create_failure_action(result), "FAILED")

    def test_explicit_seat_conflict_still_reselects(self) -> None:
        result = CreateResult(
            False,
            None,
            None,
            None,
            0,
            200,
            "22035010",
            None,
            "票被抢走",
        )

        self.assertEqual(create_failure_action(result), "RESELECT")


if __name__ == "__main__":
    unittest.main()
