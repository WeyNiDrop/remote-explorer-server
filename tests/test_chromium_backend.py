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


if __name__ == "__main__":
    unittest.main()
