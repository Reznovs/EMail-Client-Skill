#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mail_core import DEFAULT_CONFIG, EmailClientError, draft_email
from mail_tools import run_tool


def pretty_dump(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def load_html_body(args: argparse.Namespace) -> str:
    if getattr(args, "html_file", None):
        return Path(args.html_file).expanduser().read_text(encoding="utf-8")
    if getattr(args, "html", None):
        return args.html
    raise EmailClientError("must provide --html or --html-file", code="invalid_request")


def parse_inline_images(entries: list[str] | None) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for entry in entries or []:
        if "=" not in entry:
            raise EmailClientError(
                f"invalid --inline value (expected cid=path): {entry}", code="invalid_request"
            )
        cid, path = entry.split("=", 1)
        result.append({"cid": cid.strip(), "path": path.strip()})
    return result


def cmd_migrate_config(args: argparse.Namespace) -> None:
    pretty_dump(run_tool("migrate_config", {"config_path": args.config}))


def cmd_setup_account(args: argparse.Namespace) -> None:
    recipients = None
    if args.recipient:
        recipients = []
        for i, r in enumerate(args.recipient):
            email_name = r.split(":", 1) if ":" in r else (r, "")
            recipients.append({
                "email": email_name[0].strip(),
                "name": email_name[1].strip() if len(email_name) > 1 else "",
                "main": i == 0,
            })
    payload: dict[str, Any] = {
        "provider": args.provider,
        "email": args.email,
        "config_path": args.config,
    }
    if args.auth_code:
        payload["auth_code"] = args.auth_code
    if args.display_name:
        payload["display_name"] = args.display_name
    if args.login_user:
        payload["login_user"] = args.login_user
    if args.resend_api_key:
        payload["resend_api_key"] = args.resend_api_key
    if recipients:
        payload["recipients"] = recipients
    pretty_dump(run_tool("setup_account", payload))


def cmd_doctor_account(args: argparse.Namespace) -> None:
    pretty_dump(run_tool("doctor_account", {"config_path": args.config}))


def cmd_test_login(args: argparse.Namespace) -> None:
    pretty_dump(
        run_tool(
            "test_login",
            {
                "config_path": args.config,
                "imap_only": args.imap_only,
                "smtp_only": args.smtp_only,
            },
        )
    )


def cmd_list_messages(args: argparse.Namespace) -> None:
    result = run_tool(
        "list_messages",
        {
            "config_path": args.config,
            "folder": args.folder,
            "limit": args.limit,
        },
    )
    for item in result["messages"]:
        print(f"[{item['uid']}] {item['date']} | {item['from']} | {item['subject']}")


def cmd_search_messages(args: argparse.Namespace) -> None:
    result = run_tool(
        "search_messages",
        {
            "query": args.query,
            "config_path": args.config,
            "folder": args.folder,
            "scan": args.scan,
            "limit": args.limit,
        },
    )
    for item in result["messages"]:
        print(f"[{item['uid']}] {item['date']} | {item['from']} | {item['subject']}")
        if item.get("preview"):
            print(f"  {item['preview']}")


def cmd_get_message(args: argparse.Namespace) -> None:
    result = run_tool(
        "get_message",
        {
            "uid": args.uid,
            "config_path": args.config,
            "folder": args.folder,
        },
    )
    message = result["message"]
    print(f"UID: {message['uid']}")
    print(f"Date: {message['date']}")
    print(f"From: {message['from']}")
    print(f"To: {message['to']}")
    print(f"Subject: {message['subject']}")
    print("")
    print(message["body_text"] or "[no readable body]")


def cmd_download_attachments(args: argparse.Namespace) -> None:
    result = run_tool(
        "download_attachments",
        {
            "uid": args.uid,
            "mode": args.mode,
            "config_path": args.config,
            "folder": args.folder,
        },
    )
    print(f"mode: {result['mode']}")
    print(f"dir: {result['target_dir']}")
    print(f"files: {len(result['files'])}")
    for item in result["files"]:
        print(f"- {item}")


def cmd_send_email(args: argparse.Namespace) -> None:
    payload: dict[str, Any] = {
        "subject": args.subject,
        "html_body": load_html_body(args),
        "config_path": args.config,
    }
    if args.to:
        payload["to"] = args.to
    if args.attach:
        payload["attachments"] = args.attach
    inline_images = parse_inline_images(args.inline)
    if inline_images:
        payload["inline_images"] = inline_images
    if args.ics_file:
        payload["ics_file"] = args.ics_file
    if args.ics_json:
        payload["ics_event"] = json.loads(args.ics_json)
    if args.ics_filename:
        payload["ics_filename"] = args.ics_filename
    pretty_dump(run_tool("send_email", payload))


def cmd_send_scheduled_email(args: argparse.Namespace) -> None:
    payload: dict[str, Any] = {
        "subject": args.subject,
        "html_body": load_html_body(args),
        "config_path": args.config,
    }
    if args.to:
        payload["to"] = args.to
    if args.from_addr:
        payload["from_addr"] = args.from_addr
    if args.api_key:
        payload["api_key"] = args.api_key
    if args.delay_minutes is not None:
        payload["delay_minutes"] = args.delay_minutes
    pretty_dump(run_tool("send_scheduled_email", payload))


def cmd_draft_email(args: argparse.Namespace) -> None:
    body_text = ""
    if getattr(args, "body_file", None):
        body_text = Path(args.body_file).expanduser().read_text(encoding="utf-8")
    elif getattr(args, "body", None):
        body_text = args.body
    result = draft_email(
        subject=args.subject,
        body=body_text,
        output=args.output,
    )
    if result["output_path"]:
        print(f"draft_saved: {result['output_path']}")
        return
    print(result["html_draft"])


def cmd_list_folders(args: argparse.Namespace) -> None:
    pretty_dump(run_tool("list_folders", {"config_path": args.config}))


def cmd_trash_messages(args: argparse.Namespace) -> None:
    pretty_dump(run_tool("trash_messages", {
        "uids": args.uid,
        "confirmed": args.confirm,
        "folder": args.folder,
        "trash_folder": args.trash_folder,
        "config_path": args.config,
    }))


def cmd_restore_messages(args: argparse.Namespace) -> None:
    pretty_dump(run_tool("restore_messages", {
        "uids": args.uid,
        "confirmed": args.confirm,
        "target_folder": args.target_folder,
        "trash_folder": args.trash_folder,
        "config_path": args.config,
    }))


def cmd_purge_messages(args: argparse.Namespace) -> None:
    pretty_dump(run_tool("purge_messages", {
        "uids": args.uid,
        "confirmed": args.confirm,
        "trash_folder": args.trash_folder,
        "config_path": args.config,
    }))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone CLI for email-client-skill.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate_parser = subparsers.add_parser("migrate_config")
    migrate_parser.set_defaults(func=cmd_migrate_config)

    setup_parser = subparsers.add_parser("setup_account")
    setup_parser.add_argument("--provider", required=True)
    setup_parser.add_argument("--email", required=True)
    setup_parser.add_argument("--auth-code")
    setup_parser.add_argument("--display-name")
    setup_parser.add_argument("--login-user")
    setup_parser.add_argument("--resend-api-key")
    setup_parser.add_argument(
        "--recipient", action="append",
        help='收件人，格式 email[:name]（第一个自动标记为 main，可多次）',
    )
    setup_parser.set_defaults(func=cmd_setup_account)

    doctor_parser = subparsers.add_parser("doctor_account")
    doctor_parser.set_defaults(func=cmd_doctor_account)

    test_parser = subparsers.add_parser("test_login")
    test_parser.add_argument("--imap-only", action="store_true")
    test_parser.add_argument("--smtp-only", action="store_true")
    test_parser.set_defaults(func=cmd_test_login)

    list_parser = subparsers.add_parser("list_messages")
    list_parser.add_argument("--folder", default="INBOX")
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.set_defaults(func=cmd_list_messages)

    search_parser = subparsers.add_parser("search_messages")
    search_parser.add_argument("--query", default="")
    search_parser.add_argument("--folder", default="INBOX")
    search_parser.add_argument("--scan", type=int, default=200)
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.set_defaults(func=cmd_search_messages)

    get_parser = subparsers.add_parser("get_message")
    get_parser.add_argument("--uid", required=True)
    get_parser.add_argument("--folder", default="INBOX")
    get_parser.set_defaults(func=cmd_get_message)

    download_parser = subparsers.add_parser("download_attachments")
    download_parser.add_argument("--uid", required=True)
    download_parser.add_argument("--folder", default="INBOX")
    download_parser.add_argument("--mode", choices=["temp", "archive"], default="temp")
    download_parser.set_defaults(func=cmd_download_attachments)

    send_parser = subparsers.add_parser("send_email")
    send_parser.add_argument("--to", nargs="+", help="收件人（未传时自动使用 main 收件人）")
    send_parser.add_argument("--subject", required=True)
    send_parser.add_argument("--html", help="HTML 正文字符串")
    send_parser.add_argument("--html-file", help="HTML 正文文件路径")
    send_parser.add_argument("--attach", action="append", help="普通附件路径（可多次）")
    send_parser.add_argument(
        "--inline", action="append",
        help='内联图片，格式 cid=path，HTML 中用 <img src="cid:xxx"> 引用（可多次）',
    )
    send_parser.add_argument("--ics-file", help="日程 ICS 文件路径")
    send_parser.add_argument(
        "--ics-json",
        help='日程事件 JSON，字段：summary/start/end/location/description/attendees',
    )
    send_parser.add_argument("--ics-filename", default="invite.ics")
    send_parser.set_defaults(func=cmd_send_email)

    draft_parser = subparsers.add_parser("draft_email")
    draft_parser.add_argument("--subject", required=True)
    draft_parser.add_argument("--body")
    draft_parser.add_argument("--body-file")
    draft_parser.add_argument("--output")
    draft_parser.set_defaults(func=cmd_draft_email)

    scheduled_parser = subparsers.add_parser("send_scheduled_email", help="通过 Resend API 定时发送邮件")
    scheduled_parser.add_argument("--to", help="收件人地址（未传时自动使用 main 收件人）")
    scheduled_parser.add_argument("--subject", required=True, help="邮件主题")
    scheduled_parser.add_argument("--html", help="HTML 正文字符串")
    scheduled_parser.add_argument("--html-file", help="HTML 正文文件路径")
    scheduled_parser.add_argument("--from", dest="from_addr", default=None, help="发件人地址（默认读 sender.email）")
    scheduled_parser.add_argument("--delay-minutes", type=int, default=3, help="几分钟后发送（默认 3）")
    scheduled_parser.add_argument("--api-key", default=None, help="Resend API Key（默认读 sender.resend_api_key）")
    scheduled_parser.set_defaults(func=cmd_send_scheduled_email)

    folders_parser = subparsers.add_parser("list_folders", help="列出邮箱所有文件夹并识别回收站")
    folders_parser.set_defaults(func=cmd_list_folders)

    trash_parser = subparsers.add_parser("trash_messages", help="软删：移到回收站（默认预览模式，--confirm 才执行）")
    trash_parser.add_argument("--uid", action="append", required=True, help="可多次；UID 数字")
    trash_parser.add_argument("--folder", default="INBOX", help="来源文件夹")
    trash_parser.add_argument("--trash-folder", default=None, help="显式指定回收站文件夹（不传则自动识别）")
    trash_parser.add_argument("--confirm", action="store_true", help="真正执行；不加则只预览")
    trash_parser.set_defaults(func=cmd_trash_messages)

    restore_parser = subparsers.add_parser("restore_messages", help="从回收站恢复到指定文件夹")
    restore_parser.add_argument("--uid", action="append", required=True)
    restore_parser.add_argument("--target-folder", default="INBOX")
    restore_parser.add_argument("--trash-folder", default=None)
    restore_parser.add_argument("--confirm", action="store_true")
    restore_parser.set_defaults(func=cmd_restore_messages)

    purge_parser = subparsers.add_parser("purge_messages", help="硬删：仅对回收站内 UID 有效；不可恢复")
    purge_parser.add_argument("--uid", action="append", required=True)
    purge_parser.add_argument("--trash-folder", default=None)
    purge_parser.add_argument("--confirm", action="store_true")
    purge_parser.set_defaults(func=cmd_purge_messages)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except EmailClientError as exc:
        raise SystemExit(f"error[{exc.code}]: {exc.message}")


if __name__ == "__main__":
    main()
