import unittest

try:
    from remote_explorer_server.chromium_backend import ChromiumBrowserService
except ImportError:  # pragma: no cover - server UI dependencies are optional in CI test jobs.
    ChromiumBrowserService = None


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


if __name__ == "__main__":
    unittest.main()
