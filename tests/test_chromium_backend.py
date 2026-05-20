import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    from remote_explorer_server import chromium_backend
    from remote_explorer_server.chromium_backend import ChromiumBrowserService, ChromiumUnavailable
except ImportError:  # pragma: no cover - server UI dependencies are optional in CI test jobs.
    chromium_backend = None
    ChromiumBrowserService = None
    ChromiumUnavailable = None


class FakeProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def poll(self):
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None) -> None:
        return None

    def kill(self) -> None:
        self.killed = True


class FakeLogFile:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeCdpConnection:
    def __init__(self) -> None:
        self.calls = []

    def call(self, method, payload=None, timeout=None):
        del timeout
        self.calls.append((method, payload or {}))
        return {}


@unittest.skipIf(ChromiumBrowserService is None, "PySide6 is not installed")
class ChromiumBrowserServiceTests(unittest.TestCase):
    def test_close_without_connection_after_connect_failure(self) -> None:
        service = ChromiumBrowserService.__new__(ChromiumBrowserService)
        service.process = FakeProcess()
        service.browser_log_file = FakeLogFile()
        service._fullscreen_topmost = False

        service.close()

        self.assertTrue(service.process.terminated)
        self.assertFalse(service.process.killed)
        self.assertIsNone(service.browser_log_file)

    def test_init_closes_browser_log_when_connect_fails(self) -> None:
        process = FakeProcess()
        log_file = FakeLogFile()
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                browser_executable=None,
                data_dir=Path(temp_dir),
                start_url="about:blank",
            )

            def launch_browser(service, start_url):
                self.assertEqual(start_url, "about:blank")
                service.browser_log_file = log_file
                return process

            with (
                patch.object(
                    chromium_backend,
                    "find_chromium_executable",
                    return_value="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                ),
                patch.object(chromium_backend, "_free_port", return_value=9222),
                patch.object(ChromiumBrowserService, "_launch_browser", launch_browser),
                patch.object(ChromiumBrowserService, "_connect", side_effect=ChromiumUnavailable("CDP unavailable")),
            ):
                with self.assertRaises(ChromiumUnavailable):
                    ChromiumBrowserService(config)

        self.assertTrue(process.terminated)
        self.assertTrue(log_file.closed)

    def test_macos_escape_shortcut_does_not_send_text_parameter(self) -> None:
        service = ChromiumBrowserService.__new__(ChromiumBrowserService)
        service.connection = FakeCdpConnection()
        service._fullscreen_topmost = False
        with patch.object(chromium_backend.sys, "platform", "darwin"):
            self.assertTrue(service.send_key_shortcut("exit_fullscreen"))

        payloads = [payload for method, payload in service.connection.calls if method == "Input.dispatchKeyEvent"]
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0]["nativeVirtualKeyCode"], 53)
        self.assertNotIn("text", payloads[0])
        self.assertNotIn("text", payloads[1])

    def test_macos_fullscreen_shortcut_uses_platform_native_key_code(self) -> None:
        service = ChromiumBrowserService.__new__(ChromiumBrowserService)
        service.connection = FakeCdpConnection()
        service._fullscreen_topmost = False
        with patch.object(chromium_backend.sys, "platform", "darwin"):
            self.assertTrue(service.send_key_shortcut("fullscreen"))

        payloads = [payload for method, payload in service.connection.calls if method == "Input.dispatchKeyEvent"]
        self.assertEqual(payloads[0]["key"], "f")
        self.assertEqual(payloads[0]["windowsVirtualKeyCode"], 70)
        self.assertEqual(payloads[0]["nativeVirtualKeyCode"], 3)
        self.assertEqual(payloads[0]["text"], "f")
        self.assertNotIn("text", payloads[1])


if __name__ == "__main__":
    unittest.main()
