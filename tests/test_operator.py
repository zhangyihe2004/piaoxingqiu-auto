from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from piaoxingqiu_auto.runtime.operator import OperatorGateway


class FakePage:
    def is_closed(self) -> bool:
        return False


class OperatorGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_verification_releases_waiter(self) -> None:
        with patch.dict(
            os.environ,
            {"PIAOXINGQIU_OPERATOR_PUBLIC_URL": "https://example.test/verify"},
        ):
            gateway = OperatorGateway()
        access = gateway.issue(1, 2, FakePage())
        self.assertIsNotNone(access)
        session = next(iter(gateway._sessions.values()))

        waiter = asyncio.create_task(access.wait())
        session.result.set_result(True)

        await waiter

    async def test_revoked_verification_does_not_look_completed(self) -> None:
        with patch.dict(
            os.environ,
            {"PIAOXINGQIU_OPERATOR_PUBLIC_URL": "https://example.test/verify"},
        ):
            gateway = OperatorGateway()
        access = gateway.issue(1, 2, FakePage())
        self.assertIsNotNone(access)

        gateway.revoke(1, 2)

        with self.assertRaises(TimeoutError):
            await access.wait()


if __name__ == "__main__":
    unittest.main()
