"""P0: CDP BrowserContext 必须 fail closed；所有测试均离线运行。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from engine import MuseEngine, MuseGenerationError


class FakeBrowser:
    def __init__(self, fail=None, missing=None, foreign_context=False, dispose_fail=False):
        self.fail = fail
        self.missing = missing
        self.foreign_context = foreign_context
        self.dispose_fail = dispose_fail
        self.calls = []

    def send(self, method, params=None):
        self.calls.append((method, params))
        if method == self.fail or (method == "Target.disposeBrowserContext" and self.dispose_fail):
            raise RuntimeError("injected CDP failure")
        if method == "Target.createBrowserContext":
            return {"result": {} if self.missing == "context" else {"browserContextId": "ctx-a"}}
        if method == "Target.createTarget":
            return {"result": {} if self.missing == "target" else {"targetId": "target-a"}}
        if method == "Target.getTargetInfo":
            if self.missing == "info":
                return {"result": {}}
            return {"result": {"targetInfo": {
                "targetId": "target-a",
                "browserContextId": "ctx-b" if self.foreign_context else "ctx-a",
            }}}
        return {"result": {}}

    def methods(self):
        return [method for method, _ in self.calls]


class BrowserIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = SimpleNamespace(
            profile_dir=str(Path(self.tmp.name) / "profile"),
            download_dir=str(Path(self.tmp.name) / "downloads"),
            cdp_port=19231,
        )
        self.engine = MuseEngine(cfg)
        self.browser = FakeBrowser()
        self.engine.browser = self.browser

    def assert_clean(self, browser=None):
        browser = browser or self.browser
        self.assertIsNone(self.engine.page)
        self.assertIsNone(self.engine.current_context_id)
        self.assertIsNone(self.engine.current_acc_id)
        self.assertEqual(self.engine._active_contexts, set())

    @patch("requests.put", side_effect=AssertionError("禁止创建普通标签页"))
    def test_no_browser_fails_closed(self, put):
        self.engine.browser = None
        with self.assertRaisesRegex(MuseGenerationError, "CDP 未连接"):
            self.engine._open_page()
        put.assert_not_called()

    @patch("requests.put", side_effect=AssertionError("禁止创建普通标签页"))
    def test_create_context_errors_fail_closed(self, put):
        self.engine.browser = FakeBrowser(fail="Target.createBrowserContext")
        with self.assertRaisesRegex(MuseGenerationError, "禁止回退"):
            self.engine._open_page()
        self.assert_clean(self.engine.browser)
        put.assert_not_called()

    def test_missing_context_id_fails_closed(self):
        self.engine.browser = FakeBrowser(missing="context")
        with self.assertRaisesRegex(MuseGenerationError, "未返回 ID"):
            self.engine._open_page()
        self.assert_clean(self.engine.browser)

    def test_create_target_errors_disposes_context(self):
        self.engine.browser = FakeBrowser(fail="Target.createTarget")
        with self.assertRaises(MuseGenerationError):
            self.engine._open_page()
        self.assertIn("Target.disposeBrowserContext", self.engine.browser.methods())
        self.assert_clean(self.engine.browser)

    def test_missing_target_id_disposes_context(self):
        self.engine.browser = FakeBrowser(missing="target")
        with self.assertRaisesRegex(MuseGenerationError, "Target ID"):
            self.engine._open_page()
        self.assertIn("Target.disposeBrowserContext", self.engine.browser.methods())
        self.assert_clean(self.engine.browser)

    def test_context_mismatch_disposes_context_before_opening_websocket(self):
        self.engine.browser = FakeBrowser(foreign_context=True)
        with patch("engine.CDP") as cdpx:
            with self.assertRaisesRegex(MuseGenerationError, "归属校验失败"):
                self.engine._open_page()
            cdpx.assert_not_called()
        self.assertIn("Target.disposeBrowserContext", self.engine.browser.methods())
        self.assert_clean(self.engine.browser)

    def test_context_info_missing_fails_closed(self):
        self.engine.browser = FakeBrowser(missing="info")
        with self.assertRaisesRegex(MuseGenerationError, "归属校验失败"):
            self.engine._open_page()
        self.assert_clean(self.engine.browser)

    def test_page_websocket_failure_disposes_context(self):
        with patch("engine.CDP", side_effect=TimeoutError("injected websocket timeout")):
            with self.assertRaisesRegex(MuseGenerationError, "禁止回退"):
                self.engine._open_page()
        self.assertIn("Target.disposeBrowserContext", self.browser.methods())
        self.assert_clean()

    def test_page_initialization_failure_closes_page_and_context(self):
        page = Mock()
        page.send.side_effect = RuntimeError("injected Runtime.enable failure")
        with patch("engine.CDP", return_value=page):
            with self.assertRaisesRegex(MuseGenerationError, "禁止回退"):
                self.engine._open_page()
        page.close.assert_called_once()
        self.assertIn("Target.disposeBrowserContext", self.browser.methods())
        self.assert_clean()

    def test_success_uses_matching_context_and_normal_cleanup(self):
        page = Mock()
        page.send.return_value = {"result": {}}
        with patch("engine.CDP", return_value=page) as cdpx:
            got = self.engine._open_page()
        self.assertIs(got, page)
        self.assertEqual(self.engine.current_context_id, "ctx-a")
        self.assertEqual(self.engine._active_contexts, {"ctx-a"})
        self.assertIn("Target.getTargetInfo", self.browser.methods())
        self.assertEqual(cdpx.call_args.args[0],
                         "ws://127.0.0.1:19231/devtools/page/target-a")
        self.engine.page = page
        self.engine.current_acc_id = "account-a"
        self.engine._close_page()
        page.close.assert_called_once()
        self.assert_clean()

    def test_dispose_failure_retains_context_for_later_stop(self):
        page = Mock()
        self.engine.page = page
        self.engine.current_context_id = "ctx-a"
        self.engine._active_contexts.add("ctx-a")
        self.browser.dispose_fail = True
        self.engine._close_page()
        page.close.assert_called_once()
        self.assertIsNone(self.engine.current_context_id)
        self.assertIn("ctx-a", self.engine._active_contexts)

    def test_clear_cookies_failure_is_not_ignored(self):
        page = Mock()
        page.send.side_effect = RuntimeError("injected")
        with self.assertRaisesRegex(MuseGenerationError, "清理隔离"):
            self.engine._apply_cookies(page, {"hatch_sess": "dummy"})

    def test_cookie_write_requires_one_successful_domain(self):
        page = Mock()
        def answer(method, *args):
            if method == "Network.setCookie":
                raise RuntimeError("injected")
            return {}
        page.send.side_effect = answer
        with self.assertRaisesRegex(MuseGenerationError, "注入 Cookie"):
            self.engine._apply_cookies(page, {"hatch_sess": "dummy"})

    def test_cookie_write_rejects_explicit_cdp_failure(self):
        page = Mock()
        def answer(method, params=None):
            if method == "Network.setCookie":
                return {"result": {"success": False}}
            return {"result": {}}
        page.send.side_effect = answer
        with self.assertRaisesRegex(MuseGenerationError, "注入 Cookie"):
            self.engine._apply_cookies(page, {"hatch_sess": "dummy"})

    def test_cookie_write_allows_one_valid_domain(self):
        page = Mock()
        def answer(method, params=None):
            if method == "Network.setCookie" and params["domain"] == ".muse.ai":
                raise RuntimeError("one domain unavailable")
            return {"result": {}}
        page.send.side_effect = answer
        self.engine._apply_cookies(page, {"hatch_sess": "dummy"})

    def test_ensure_page_cookie_failure_cleans_up_new_page(self):
        page = Mock()
        page.send.return_value = {"result": {}}
        with (patch("engine.CDP", return_value=page),
              patch.object(self.engine, "renew_session_http", return_value={}),
              patch.object(self.engine, "_apply_cookies",
                           side_effect=MuseGenerationError("injected cookie failure"))):
            with self.assertRaisesRegex(MuseGenerationError, "injected cookie"):
                self.engine.ensure_page({"hatch_sess": "dummy"}, account_id="account-a")
        page.close.assert_called_once()
        self.assertIn("Target.disposeBrowserContext", self.browser.methods())
        self.assert_clean()

    def test_ensure_page_navigation_failure_cleans_up_new_page(self):
        page = Mock()
        def answer(method, params=None):
            if method == "Page.navigate":
                raise RuntimeError("navigation failed")
            return {"result": {}}
        page.send.side_effect = answer
        with (patch("engine.CDP", return_value=page),
              patch.object(self.engine, "renew_session_http", return_value={})):
            with self.assertRaisesRegex(RuntimeError, "navigation failed"):
                self.engine.ensure_page({"hatch_sess": "dummy"}, account_id="account-a")
        page.close.assert_called_once()
        self.assertIn("Target.disposeBrowserContext", self.browser.methods())
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
