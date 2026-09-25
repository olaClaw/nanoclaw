import importlib.util
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path


module_path = Path(__file__).with_name("nextcloud-calendar-broker.py")
spec = importlib.util.spec_from_file_location("calendar_broker", module_path)
broker = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = broker
spec.loader.exec_module(broker)


class FakeResponse:
    status = 207

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self, limit):
        return self.body[:limit]


class CalendarBrokerTests(unittest.TestCase):
    def setUp(self):
        self.config = broker.Config("https://calendar.example/remote.php/dav/calendars/user/personal/", "user", "demo-password", "demo-token")

    def test_only_bounded_report_is_sent_with_tls_and_auth(self):
        calls = []
        body = b'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:propstat><d:prop><c:calendar-data>BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Demo\nEND:VEVENT\nEND:VCALENDAR</c:calendar-data></d:prop></d:propstat></d:response></d:multistatus>'

        def opener(request, **options):
            calls.append((request, options))
            return FakeResponse(body)

        result = broker.list_events(self.config, "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z", opener)
        self.assertEqual(len(result["events"]), 1)
        self.assertIn("untrusted", result["notice"])
        request, options = calls[0]
        self.assertEqual(request.get_method(), "REPORT")
        self.assertEqual(request.full_url, self.config.calendar_url)
        self.assertIn(b"20261001T000000Z", request.data)
        self.assertIn(b"20261002T000000Z", request.data)
        self.assertEqual(options["timeout"], 20)
        self.assertEqual(request.get_header("Depth"), "1")
        self.assertTrue(request.get_header("Authorization").startswith("Basic "))

    def test_invalid_range_never_reaches_calendar(self):
        def forbidden(*_args, **_kwargs):
            self.fail("network request was attempted")

        for start, end in [
            ("2026-10-02T00:00:00Z", "2026-10-01T00:00:00Z"),
            ("2026-10-01T00:00:00Z", "2027-01-10T00:00:00Z"),
            ("2026-10-01T00:00:00", "2026-10-02T00:00:00Z"),
        ]:
            with self.assertRaises(broker.BrokerError):
                broker.list_events(self.config, start, end, forbidden)

    def test_only_read_tool_can_be_called(self):
        result = broker.handle_rpc(self.config, {"id": 1, "method": "tools/call", "params": {"name": "nextcloud_create_event", "arguments": {}}})
        self.assertTrue(result["result"]["isError"])
        result = broker.handle_rpc(self.config, {"id": 2, "method": "tools/call", "params": {"name": "nextcloud_list_events", "arguments": {"start": "a", "end": "b", "url": "https://evil.example/"}}})
        self.assertTrue(result["result"]["isError"])

    def test_config_requires_private_regular_file_and_https(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "calendar.json"
            payload = {"calendar_url": self.config.calendar_url, "username": "user", "app_password": "demo-password", "broker_token": "demo-token"}
            path.write_text(json.dumps(payload))
            path.chmod(0o600)
            self.assertEqual(broker.load_config(path).port, 18766)
            path.chmod(0o644)
            with self.assertRaises(broker.BrokerError):
                broker.load_config(path)
            path.chmod(0o600)
            payload["calendar_url"] = "http://calendar.example/"
            path.write_text(json.dumps(payload))
            with self.assertRaises(broker.BrokerError):
                broker.load_config(path)


if __name__ == "__main__":
    unittest.main()
