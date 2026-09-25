from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name("infomaniak_mail_broker.py")
INSTALLER_PATH = Path(__file__).with_name("install_service.py")
SPEC = importlib.util.spec_from_file_location("infomaniak_mail_broker", MODULE_PATH)
assert SPEC and SPEC.loader
broker_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = broker_module
SPEC.loader.exec_module(broker_module)


class FakeImap:
    def __init__(self, raw_message: bytes):
        self.raw_message = raw_message
        self.calls: list[tuple[object, ...]] = []

    def uid(self, *args: object):
        self.calls.append(args)
        command = str(args[0]).upper()
        if command == "SEARCH":
            return "OK", [b"40 41"]
        request = str(args[-1])
        if "BODY.PEEK[HEADER.FIELDS" in request:
            header, _, _ = self.raw_message.partition(b"\n\n")
            metadata = b"1 (UID 41 FLAGS () RFC822.SIZE 500 BODYSTRUCTURE (ATTACHMENT))"
            return "OK", [(metadata, header + b"\n\n"), b")"]
        if request == "(RFC822.SIZE)":
            return "OK", [f"1 (UID 41 RFC822.SIZE {len(self.raw_message)})".encode()]
        if request == "(BODY.PEEK[])":
            return "OK", [(b"1 (UID 41 BODY[]", self.raw_message), b")"]
        raise AssertionError(args)


def fixture_message(filename: str = "report.pdf") -> bytes:
    message = EmailMessage()
    message["Subject"] = "Quarterly report"
    message["From"] = "Alice <alice@example.test>"
    message["To"] = "Bob <bob@example.test>"
    message["Date"] = "Mon, 21 Sep 2026 10:00:00 +0200"
    message["Message-ID"] = "<fixture@example.test>"
    message.set_content("This is untrusted email body text.")
    message.add_attachment(b"PDF fixture", maintype="application", subtype="pdf", filename=filename)
    return message.as_bytes()


class BrokerTests(unittest.TestCase):
    def test_default_bind_does_not_expose_the_broker_on_the_network(self):
        self.assertEqual(broker_module.DEFAULT_BIND, "localhost")

    def config(self, root: Path, **changes: object):
        values = dict(
            email_address="mailbox@example.test",
            device_password="device-password",
            broker_token="broker-token",
            bind="127.0.0.1",
            port=18765,
            download_dir=root / "downloads",
            max_message_bytes=1024 * 1024,
            max_attachment_bytes=1024 * 1024,
        )
        values.update(changes)
        return broker_module.Config(**values)

    def test_config_is_private_and_tokens_do_not_enter_tool_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config" / "mail.json"
            config = self.config(Path(temporary))
            broker_module.write_config(path, config)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            loaded = broker_module.load_config(path)
            self.assertEqual(loaded.device_password, "device-password")
            tools = json.dumps(broker_module.TOOLS)
            self.assertNotIn("device-password", tools)
            self.assertNotIn("broker-token", tools)

    def test_only_read_tools_are_exposed(self):
        names = " ".join(tool["name"] for tool in broker_module.TOOLS)
        for forbidden in ("send", "delete", "move", "reply", "forward", "mark", "draft"):
            self.assertNotIn(forbidden, names)
        source = MODULE_PATH.read_text(encoding="utf-8").lower()
        self.assertNotIn("import smtplib", source)
        self.assertNotIn('.store(', source)
        self.assertIn('select("inbox", readonly=true)', source)

    def test_service_hardening_is_compatible_with_unprivileged_lxc(self):
        source = INSTALLER_PATH.read_text(encoding="utf-8")
        for unsupported in (
            "PrivateDevices=true",
            "ProtectKernelModules=true",
            "ProtectKernelLogs=true",
        ):
            self.assertNotIn(unsupported, source)
        for retained in (
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=strict",
            "ProtectHome=read-only",
            "RestrictSUIDSGID=true",
        ):
            self.assertIn(retained, source)

    def test_body_reads_use_peek_and_are_labeled_untrusted(self):
        with tempfile.TemporaryDirectory() as temporary:
            fake = FakeImap(fixture_message())

            @contextmanager
            def session(_config):
                yield fake

            broker = broker_module.MailBroker(self.config(Path(temporary)), session)
            result = broker.get_message("41")
            self.assertIn("untrusted", result["securityNotice"].lower())
            fetches = " ".join(str(call[-1]) for call in fake.calls)
            self.assertIn("BODY.PEEK[]", fetches)
            self.assertNotIn("(BODY[])", fetches)

    def test_attachment_download_and_executable_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            @contextmanager
            def pdf_session(_config):
                yield FakeImap(fixture_message())

            broker = broker_module.MailBroker(self.config(root), pdf_session)
            result = broker.download_attachment("41", 1)
            saved_files = list((root / "downloads").iterdir())
            self.assertEqual(len(saved_files), 1)
            saved = saved_files[0]
            self.assertTrue(saved.is_file())
            self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
            self.assertEqual(
                result["attachment"]["path"],
                f"/workspace/agent/downloads/infomaniak/{saved.name}",
            )

            @contextmanager
            def exe_session(_config):
                yield FakeImap(fixture_message("invoice.exe"))

            unsafe = broker_module.MailBroker(self.config(root), exe_session)
            with self.assertRaisesRegex(broker_module.BrokerError, "blocked"):
                unsafe.download_attachment("41", 1)

    def test_rpc_lists_tools_without_calling_imap(self):
        with tempfile.TemporaryDirectory() as temporary:
            broker = broker_module.MailBroker(self.config(Path(temporary)))
            result = broker_module.handle_rpc(
                broker,
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            self.assertIn("infomaniak_get_message", json.dumps(result))

    def test_http_mcp_requires_the_local_bearer_capability(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = self.config(Path(temporary), port=0)
            broker = broker_module.MailBroker(config)
            try:
                server = broker_module.ThreadingHTTPServer(
                    ("127.0.0.1", 0), broker_module.handler_class(config, broker)
                )
            except PermissionError:
                self.skipTest("sandbox does not allow loopback sockets")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            endpoint = f"http://127.0.0.1:{server.server_port}/mcp"
            payload = json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            ).encode()
            try:
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(
                        urllib.request.Request(endpoint, data=payload, method="POST"), timeout=2
                    )
                self.assertEqual(denied.exception.code, 401)

                request = urllib.request.Request(
                    endpoint,
                    data=payload,
                    method="POST",
                    headers={"Authorization": "Bearer broker-token"},
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    result = json.load(response)
                self.assertIn("infomaniak_get_message", json.dumps(result))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_login_selects_inbox_read_only(self):
        calls: list[tuple[object, ...]] = []

        class LoginImap:
            def __init__(self, *args, **kwargs):
                calls.append(("connect", *args))

            def login(self, username, password):
                calls.append(("login", username, password))
                return "OK", []

            def select(self, mailbox, readonly=False):
                calls.append(("select", mailbox, readonly))
                return "OK", []

            def logout(self):
                calls.append(("logout",))

        with tempfile.TemporaryDirectory() as temporary:
            config = self.config(Path(temporary))
            with patch.object(broker_module.imaplib, "IMAP4_SSL", LoginImap):
                with broker_module.imap_session(config):
                    pass
        self.assertIn(("select", "INBOX", True), calls)


if __name__ == "__main__":
    unittest.main()
