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


class FakeMailClient:
    def __init__(self, account):
        self.account = account
        self.folder = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def select_folder(self, folder: str, readonly: bool = True) -> None:
        self.folder = folder
        self.readonly = readonly

    def list_folders(self) -> list[dict[str, str]]:
        return [
            {"name": "INBOX", "raw_name": "INBOX", "attrs": "\\HasNoChildren", "delimiter": "/"},
            {"name": "Deleted Messages", "raw_name": "Deleted Messages", "attrs": "\\HasNoChildren", "delimiter": "/"},
        ]

    def move_uids(self, uids, dest_folder: str) -> None:
        self.__class__.moved = {"uids": [u.decode() for u in uids], "dest": dest_folder}

    def store_flags(self, uids, flags: str, operation: str = "+") -> None:
        self.__class__.flagged = {"uids": [u.decode() for u in uids], "flags": flags, "op": operation}

    def expunge_uids(self, uids) -> None:
        self.__class__.expunged = [u.decode() for u in uids]

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


def _new_config(path: Path, *, auth_code: str = "real-secret") -> Path:
    """创建新格式配置文件，返回路径。"""
    path.write_text(
        json.dumps({
            "setup": 1,
            "sender": {
                "email": "user@example.com",
                "login_user": "user@example.com",
                "display_name": "Test User",
                "provider": "gmail",
                "auth_code": auth_code,
                "imap": {"host": "imap.gmail.com", "port": 993, "security": "ssl"},
                "smtp": {"host": "smtp.gmail.com", "port": 465, "security": "ssl"},
            },
            "recipients": [
                {"email": "main@example.com", "name": "Main", "main": True},
                {"email": "other@example.com", "name": "Other", "main": False},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


class MailToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeSMTP.sent_messages = []

    def test_setup_account_creates_new_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "accounts.json"
            result = mail_core.setup_account(
                provider="gmail",
                email="user@example.com",
                auth_code="real-secret",
                display_name="Test User",
                config_path=config_path,
                recipients=[
                    {"email": "main@example.com", "name": "Main", "main": True},
                    {"email": "other@example.com", "name": "Other"},
                ],
            )
            written = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(written["setup"], 1)
        self.assertEqual(written["sender"]["email"], "user@example.com")
        self.assertEqual(written["sender"]["auth_code"], "real-secret")
        self.assertEqual(len(written["recipients"]), 2)
        self.assertTrue(written["recipients"][0]["main"])

    def test_setup_account_no_recipients(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config" / "accounts.json"
            result = mail_core.setup_account(
                provider="gmail",
                email="user@example.com",
                config_path=config_path,
            )
            written = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(written["recipients"], [])

    def test_check_setup_blocks_when_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "accounts.json"
            config_path.write_text('{"setup": 0}', encoding="utf-8")
            with self.assertRaises(mail_core.EmailClientError) as ctx:
                mail_core.check_setup(config_path)
            self.assertEqual(ctx.exception.code, "not_configured")

    def test_check_setup_passes_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "accounts.json"
            _new_config(config_path)
            data = mail_core.check_setup(config_path)
            self.assertEqual(data["setup"], 1)

    def test_get_main_recipient(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "accounts.json"
            _new_config(config_path)
            main = mail_core.get_main_recipient(config_path)
            self.assertEqual(main["email"], "main@example.com")
            self.assertEqual(main["name"], "Main")

    def test_get_main_recipient_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "accounts.json"
            path = config_path
            path.write_text(json.dumps({
                "setup": 1,
                "sender": {"email": "x@x.com"},
                "recipients": [],
            }), encoding="utf-8")
            with self.assertRaises(mail_core.EmailClientError) as ctx:
                mail_core.get_main_recipient(config_path)
            self.assertEqual(ctx.exception.code, "invalid_config")

    def test_doctor_account_new_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "accounts.json"
            _new_config(config_path)
            doctor = mail_core.doctor_account(config_path)
        self.assertEqual(doctor["doctor_status"], "ok")

    def test_doctor_account_detects_missing_auth_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "accounts.json"
            _new_config(config_path, auth_code="")
            doctor = mail_core.doctor_account(config_path)
        self.assertEqual(doctor["doctor_status"], "needs_attention")
        self.assertIn("auth_code", doctor["issues"][0])

    def test_list_search_get_and_download_use_fake_mailbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "accounts.json"
            _new_config(config_path)
            with mock.patch.object(mail_core, "MailClient", FakeMailClient):
                listed = mail_core.list_messages(config_path=config_path)
                searched = mail_core.search_messages(query="invoice", config_path=config_path)
                fetched = mail_core.get_message(uid="101", config_path=config_path)
                downloaded = mail_core.download_attachments(uid="101", config_path=config_path)

        self.assertEqual(len(listed["messages"]), 2)
        self.assertEqual(searched["messages"][0]["uid"], "101")
        self.assertEqual(fetched["message"]["uid"], "101")
        self.assertEqual(len(downloaded["files"]), 1)

    def test_send_email_uses_main_recipient(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "accounts.json"
            _new_config(config_path)
            with mock.patch.object(mail_core.smtplib, "SMTP_SSL", FakeSMTP):
                result = mail_core.send_email_tool(
                    subject="Test",
                    html_body="<p>Hello</p>",
                    config_path=config_path,
                )

        self.assertEqual(result["status"], "sent")
        self.assertEqual(len(FakeSMTP.sent_messages), 1)

    def test_send_email_explicit_to(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "accounts.json"
            attachment_dir = Path(tmpdir) / "approved"
            attachment_dir.mkdir()
            attachment_path = attachment_dir / "note.txt"
            attachment_path.write_text("hello", encoding="utf-8")
            mail_core._register_saved_attachments(attachment_dir, [attachment_path])
            _new_config(config_path)

            with mock.patch.object(mail_core.smtplib, "SMTP_SSL", FakeSMTP):
                result = mail_core.send_email_tool(
                    to=["alice@example.com"],
                    subject="Test",
                    html_body="<p>Hello</p>",
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
                output=str(output_path),
            )
            self.assertTrue(output_path.exists())

        self.assertEqual(result["output_path"], str(output_path))
        self.assertIn("Project update", result["html_draft"])

    def test_tool_runner_dispatches_setup_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "accounts.json"
            result = mail_tools.run_tool(
                "setup_account",
                {
                    "provider": "gmail",
                    "email": "user@example.com",
                    "config_path": str(config_path),
                },
            )

        self.assertEqual(result["status"], "ok")

    def test_tool_runner_blocks_when_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "accounts.json"
            config_path.write_text('{"setup": 0}', encoding="utf-8")
            with self.assertRaises(mail_core.EmailClientError) as ctx:
                mail_tools.run_tool("list_messages", {"config_path": str(config_path)})
            self.assertEqual(ctx.exception.code, "not_configured")

    def _setup_mailbox(self, tmpdir):
        config_path = Path(tmpdir) / "accounts.json"
        _new_config(config_path)
        return config_path

    def test_trash_preview_does_not_move(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._setup_mailbox(tmpdir)
            FakeMailClient.moved = None
            with mock.patch.object(mail_core, "MailClient", FakeMailClient), \
                 mock.patch.object(mail_core, "_append_audit", lambda *_a, **_k: None):
                result = mail_core.trash_messages(
                    uids=["101"], confirmed=False, config_path=config_path
                )
        self.assertEqual(result["status"], "preview")
        self.assertEqual(result["trash_folder"], "Deleted Messages")
        self.assertIsNone(FakeMailClient.moved)
        self.assertEqual(result["messages"][0]["subject"], "Invoice follow-up")

    def test_trash_confirmed_moves_and_audits(self) -> None:
        audit: list[dict] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._setup_mailbox(tmpdir)
            FakeMailClient.moved = None
            with mock.patch.object(mail_core, "MailClient", FakeMailClient), \
                 mock.patch.object(mail_core, "_append_audit", lambda e: audit.append(e)):
                result = mail_core.trash_messages(
                    uids=["101"], confirmed=True, config_path=config_path
                )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(FakeMailClient.moved, {"uids": ["101"], "dest": "Deleted Messages"})
        self.assertEqual(audit[0]["action"], "trash")
        self.assertIn("Deleted Messages", result["note"])

    def test_trash_refuses_when_source_is_trash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._setup_mailbox(tmpdir)
            with mock.patch.object(mail_core, "MailClient", FakeMailClient):
                with self.assertRaises(mail_core.EmailClientError) as ctx:
                    mail_core.trash_messages(
                        uids=["101"], confirmed=True,
                        folder="Deleted Messages", config_path=config_path,
                    )
        self.assertIn("already the trash", str(ctx.exception.message))

    def test_purge_preview_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._setup_mailbox(tmpdir)
            FakeMailClient.expunged = None
            with mock.patch.object(mail_core, "MailClient", FakeMailClient), \
                 mock.patch.object(mail_core, "_append_audit", lambda *_a, **_k: None):
                result = mail_core.purge_messages(
                    uids=["101"], confirmed=False, config_path=config_path
                )
        self.assertEqual(result["status"], "preview")
        self.assertIsNone(FakeMailClient.expunged)

    def test_purge_confirmed_expunges(self) -> None:
        audit: list[dict] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._setup_mailbox(tmpdir)
            FakeMailClient.expunged = None
            FakeMailClient.flagged = None
            with mock.patch.object(mail_core, "MailClient", FakeMailClient), \
                 mock.patch.object(mail_core, "_append_audit", lambda e: audit.append(e)):
                result = mail_core.purge_messages(
                    uids=["101"], confirmed=True, config_path=config_path
                )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(FakeMailClient.flagged["flags"], "(\\Deleted)")
        self.assertEqual(FakeMailClient.expunged, ["101"])
        self.assertEqual(audit[0]["action"], "purge")

    def test_restore_moves_out_of_trash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._setup_mailbox(tmpdir)
            FakeMailClient.moved = None
            with mock.patch.object(mail_core, "MailClient", FakeMailClient), \
                 mock.patch.object(mail_core, "_append_audit", lambda *_a, **_k: None):
                result = mail_core.restore_messages(
                    uids=["101"], confirmed=True,
                    target_folder="INBOX", config_path=config_path,
                )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(FakeMailClient.moved, {"uids": ["101"], "dest": "INBOX"})

    def test_list_folders_detects_trash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._setup_mailbox(tmpdir)
            with mock.patch.object(mail_core, "MailClient", FakeMailClient):
                result = mail_core.list_folders(config_path=config_path)
        self.assertEqual(result["trash_folder"], "Deleted Messages")
        self.assertIn({"name": "INBOX", "raw_name": "INBOX", "attrs": "\\HasNoChildren", "delimiter": "/"}, result["folders"])


if __name__ == "__main__":
    unittest.main()
