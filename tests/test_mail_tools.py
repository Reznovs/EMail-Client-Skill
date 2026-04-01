from __future__ import annotations

import json
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import mail_core
import mail_tools


class DummyKeyring:
    store: dict[tuple[str, str], str] = {}

    @classmethod
    def set_password(cls, service: str, name: str, secret: str) -> None:
        cls.store[(service, name)] = secret

    @classmethod
    def get_password(cls, service: str, name: str) -> str | None:
        return cls.store.get((service, name))

    @classmethod
    def delete_password(cls, service: str, name: str) -> None:
        cls.store.pop((service, name), None)


class FakeMailClient:
    def __init__(self, account):
        self.account = account
        self.folder = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def select_folder(self, folder: str) -> None:
        self.folder = folder

    def search_all_uids(self) -> list[bytes]:
        return [b"101", b"102"]

    def fetch_headers(self, uid: bytes) -> dict[str, str]:
        mapping = {
            b"101": {
                "uid": "101",
                "date": "Mon, 30 Mar 2026 10:00:00 +0800",
                "from": "Alice <alice@example.com>",
                "subject": "Invoice follow-up",
            },
            b"102": {
                "uid": "102",
                "date": "Mon, 30 Mar 2026 11:00:00 +0800",
                "from": "Bob <bob@example.com>",
                "subject": "Status update",
            },
        }
        return mapping[uid]

    def fetch_message(self, uid: bytes):
        msg = EmailMessage()
        if uid == b"101":
            msg["Date"] = "Mon, 30 Mar 2026 10:00:00 +0800"
            msg["From"] = "Alice <alice@example.com>"
            msg["To"] = "User <user@example.com>"
            msg["Subject"] = "Invoice follow-up"
            msg.set_content("Please review the invoice attachment.")
        else:
            msg["Date"] = "Mon, 30 Mar 2026 11:00:00 +0800"
            msg["From"] = "Bob <bob@example.com>"
            msg["To"] = "User <user@example.com>"
            msg["Subject"] = "Status update"
            msg.set_content("The weekly status is green.")
        msg.add_attachment(b"hello", maintype="application", subtype="octet-stream", filename="note.txt")
        return msg


class FakeSMTP:
    sent_messages = []

    def __init__(self, host: str, port: int, **kwargs) -> None:
        self.host = host
        self.port = port

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def ehlo(self):
        return None

    def starttls(self, context=None):
        return None

    def login(self, login_user: str, secret: str) -> None:
        self.login_user = login_user
        self.secret = secret

    def send_message(self, msg) -> None:
        self.__class__.sent_messages.append(msg)


class MailToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        DummyKeyring.store = {}
        FakeSMTP.sent_messages = []

    def test_migrate_config_creates_v2_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "accounts.json"
            config_path.write_text(
                json.dumps(
                    {
                        "accounts": [
                            {
                                "name": "work",
                                "provider": "gmail",
                                "email": "user@example.com",
                                "login_user": "user@example.com",
                                "display_name": "User",
                                "auth_mode": "app_password",
                                "auth_secret": "real-secret",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = mail_core.migrate_config(config_path)
            written = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(result["migration_status"], "migrated")
        self.assertEqual(written["version"], 2)
        self.assertIn("work", written["accounts"])

    def test_setup_account_create_and_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "accounts.json"
            with mock.patch.object(mail_core, "KEYRING_AVAILABLE", False):
                created = mail_core.setup_account(
                    account="work",
                    provider="gmail",
                    email="user@example.com",
                    config_path=config_path,
                    display_name="User One",
                    auth_secret="real-secret",
                )
                updated = mail_core.setup_account(
                    account="work",
                    provider="gmail",
                    email="user@example.com",
                    config_path=config_path,
                    display_name="User Two",
                )
            written = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(created["status"], "ok")
        self.assertEqual(updated["status"], "ok")
        self.assertEqual(written["accounts"]["work"]["identity"]["display_name"], "User Two")

    def test_doctor_account_detects_placeholder_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "accounts.json"
            mail_core.setup_account(
                account="work",
                provider="gmail",
                email="user@example.com",
                config_path=config_path,
            )
            doctor = mail_core.doctor_account(config_path)

        self.assertEqual(doctor["doctor_status"], "needs_attention")
        self.assertIn("placeholder", doctor["accounts"][0]["issues"][0])

    def test_test_login_reports_both_channels(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "accounts.json"
            with mock.patch.object(mail_core, "KEYRING_AVAILABLE", False):
                mail_core.setup_account(
                    account="work",
                    provider="gmail",
                    email="user@example.com",
                    config_path=config_path,
                    auth_secret="real-secret",
                )
            with mock.patch.object(mail_core, "test_imap_login", return_value=None), mock.patch.object(
                mail_core, "test_smtp_login", return_value=None
            ):
                result = mail_core.test_login(account="work", config_path=config_path)

        self.assertEqual(result["test_login_status"], "ok")
        self.assertTrue(result["imap"]["ok"])
        self.assertTrue(result["smtp"]["ok"])

    def test_list_search_get_and_download_use_fake_mailbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "accounts.json"
            with mock.patch.object(mail_core, "KEYRING_AVAILABLE", False):
                mail_core.setup_account(
                    account="work",
                    provider="gmail",
                    email="user@example.com",
                    config_path=config_path,
                    auth_secret="real-secret",
                )
            with mock.patch.object(mail_core, "MailClient", FakeMailClient):
                listed = mail_core.list_messages(account="work", config_path=config_path)
                searched = mail_core.search_messages(account="work", query="invoice", config_path=config_path)
                fetched = mail_core.get_message(account="work", uid="101", config_path=config_path)
                downloaded = mail_core.download_attachments(account="work", uid="101", config_path=config_path)

        self.assertEqual(len(listed["messages"]), 2)
        self.assertEqual(searched["messages"][0]["uid"], "101")
        self.assertEqual(fetched["message"]["uid"], "101")
        self.assertEqual(len(downloaded["files"]), 1)

    def test_send_email_uses_approved_attachment_and_smtp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "accounts.json"
            attachment_dir = Path(tmpdir) / "approved"
            attachment_dir.mkdir()
            attachment_path = attachment_dir / "note.txt"
            attachment_path.write_text("hello", encoding="utf-8")
            mail_core._register_saved_attachments(attachment_dir, [attachment_path])

            with mock.patch.object(mail_core, "KEYRING_AVAILABLE", False):
                mail_core.setup_account(
                    account="work",
                    provider="gmail",
                    email="user@example.com",
                    config_path=config_path,
                    auth_secret="real-secret",
                )

            with mock.patch.object(mail_core.smtplib, "SMTP_SSL", FakeSMTP):
                result = mail_core.send_email_tool(
                    account="work",
                    to=["alice@example.com"],
                    subject="Test",
                    body="Hello",
                    attachments=[str(attachment_path)],
                    config_path=config_path,
                )

        self.assertEqual(result["status"], "sent")
        self.assertEqual(len(FakeSMTP.sent_messages), 1)

    def test_draft_email_can_write_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "draft.txt"
            result = mail_core.draft_email(
                subject="Project update",
                body="The work is on track.",
                tone="formal",
                to_name="Alex",
                sender_name="Pat",
                output=str(output_path),
            )
            self.assertTrue(output_path.exists())

        self.assertEqual(result["output_path"], str(output_path))
        self.assertIn("Project update", result["draft"])

    def test_tool_runner_returns_structured_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "accounts.json"
            with mock.patch.object(mail_core, "KEYRING_AVAILABLE", False):
                result = mail_tools.run_tool(
                    "setup_account",
                    {
                        "account": "work",
                        "provider": "gmail",
                        "email": "user@example.com",
                        "config_path": str(config_path),
                    },
                )

        self.assertEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()
