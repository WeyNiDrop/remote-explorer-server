import threading
import time
import unittest

from remote_explorer_server.cdp import CdpConnection, CdpError


class EventOnlyWebSocket:
    def __init__(self) -> None:
        self.receive_calls = 0
        self.closed = False

    def send_json(self, payload):
        del payload

    def recv_json(self, timeout):
        del timeout
        self.receive_calls += 1
        return {"method": "Page.lifecycleEvent"}

    def close(self):
        self.closed = True


class CdpConnectionTests(unittest.TestCase):
    def test_call_timeout_is_total_deadline_even_when_events_keep_arriving(self) -> None:
        connection = CdpConnection.__new__(CdpConnection)
        connection.websocket = EventOnlyWebSocket()
        connection.next_id = 0
        connection.lock = threading.Lock()
        connection.closed = threading.Event()

        started = time.monotonic()
        with self.assertRaisesRegex(CdpError, "timed out"):
            connection.call("Page.captureScreenshot", timeout=0.02)

        self.assertLess(time.monotonic() - started, 0.5)
        self.assertGreater(connection.websocket.receive_calls, 0)

    def test_call_times_out_while_another_command_holds_the_connection(self) -> None:
        connection = CdpConnection.__new__(CdpConnection)
        connection.websocket = EventOnlyWebSocket()
        connection.next_id = 0
        connection.lock = threading.Lock()
        connection.closed = threading.Event()
        connection.lock.acquire()
        try:
            with self.assertRaisesRegex(CdpError, "waiting for the CDP connection"):
                connection.call("Runtime.evaluate", timeout=0.01)
        finally:
            connection.lock.release()

    def test_close_marks_connection_closed(self) -> None:
        connection = CdpConnection.__new__(CdpConnection)
        connection.websocket = EventOnlyWebSocket()
        connection.next_id = 0
        connection.lock = threading.Lock()
        connection.closed = threading.Event()

        connection.close()

        self.assertTrue(connection.closed.is_set())
        self.assertTrue(connection.websocket.closed)
        with self.assertRaisesRegex(CdpError, "closed"):
            connection.call("Runtime.evaluate", timeout=0.01)


if __name__ == "__main__":
    unittest.main()
