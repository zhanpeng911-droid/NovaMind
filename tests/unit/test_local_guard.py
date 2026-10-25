"""A0 本机安全基线测试：Host/Origin 白名单 + 会话令牌。"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from novamind.webui.local_guard import SESSION_COOKIE, session_token
from novamind.webui.server import app


class TestLocalGuard(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @staticmethod
    def _enabled():
        return mock.patch.dict(os.environ, {"NOVAMIND_LOCAL_GUARD": "1"})

    def test_guard_disabled_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOVAMIND_LOCAL_GUARD", None)
            resp = self.client.get("/health", headers={"host": "evil.example"})
        self.assertEqual(resp.status_code, 200)

    def test_host_spoof_rejected(self):
        with self._enabled():
            resp = self.client.get("/health", headers={"host": "evil.example"})
        self.assertEqual(resp.status_code, 403)

    def test_cross_origin_rejected(self):
        with self._enabled():
            resp = self.client.get(
                "/health",
                headers={"host": "127.0.0.1:8765", "origin": "http://evil.example"},
            )
        self.assertEqual(resp.status_code, 403)

    def test_write_without_token_rejected(self):
        with self._enabled():
            resp = self.client.delete(
                "/sessions/none", headers={"host": "127.0.0.1:8765"}
            )
        self.assertEqual(resp.status_code, 403)

    def test_cookie_issued_then_write_allowed(self):
        with self._enabled():
            first = self.client.get("/health", headers={"host": "127.0.0.1:8765"})
            self.assertEqual(first.status_code, 200)
            self.assertIn(SESSION_COOKIE, first.headers.get("set-cookie", ""))
            second = self.client.delete(
                "/sessions/none", headers={"host": "127.0.0.1:8765"}
            )
        self.assertEqual(second.status_code, 200)

    def test_header_token_allowed(self):
        with self._enabled():
            resp = self.client.delete(
                "/sessions/none",
                headers={
                    "host": "127.0.0.1:8765",
                    "x-novamind-token": session_token(),
                },
            )
        self.assertEqual(resp.status_code, 200)

    def test_restart_replaces_stale_cookie_before_next_write(self):
        with self._enabled():
            first = self.client.get("/health", headers={
                "host": "127.0.0.1:8765", "cookie": f"{SESSION_COOKIE}=previous-process"})
            self.assertIn(SESSION_COOKIE, first.headers.get("set-cookie", ""))
            second = self.client.delete("/sessions/none", headers={"host": "127.0.0.1:8765"})
        self.assertEqual(second.status_code, 200)

    def test_stale_cookie_cannot_authorize_write(self):
        with self._enabled():
            response = self.client.delete("/sessions/none", headers={
                "host": "127.0.0.1:8765", "cookie": f"{SESSION_COOKIE}=previous-process"})
        self.assertEqual(response.status_code, 403)

    def test_cross_origin_cannot_refresh_stale_cookie(self):
        with self._enabled():
            response = self.client.get("/health", headers={
                "host": "127.0.0.1:8765", "origin": "https://evil.example",
                "cookie": f"{SESSION_COOKIE}=previous-process"})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("set-cookie", response.headers)


if __name__ == "__main__":
    unittest.main()
