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


def load_body(args: argparse.Namespace) -> str:
    if getattr(args, "body_file", None):
        return Path(args.body_file).expanduser().read_text(encoding="utf-8")
    if getattr(args, "body", None):
        return args.body
    raise EmailClientError("must provide --body or --body-file", code="invalid_request")


def cmd_migrate_config(args: argparse.Namespace) -> None:
    pretty_dump(run_tool("migrate_config", {"config_path": args.config}))


def cmd_setup_account(args: argparse.Namespace) -> None:
    payload = {
        "account": args.account,
        "provider": args.provider,
        "email": args.email,
        "config_path": args.config,
        "login_user": args.login_user,
        "display_name": args.display_name,
        "auth_mode": args.auth_mode,
        "auth_secret": args.auth_secret,
        "imap_host": args.imap_host,
        "imap_port": args.imap_port,
        "imap_no_ssl": args.imap_no_ssl,
        "imap_starttls": args.imap_starttls,
        "smtp_host": args.smtp_host,
        "smtp_port": args.smtp_port,
        "smtp_no_ssl": args.smtp_no_ssl,
        "smtp_starttls": args.smtp_starttls,
        "proxy_type": args.proxy_type,
        "proxy_host": args.proxy_host,
        "proxy_port": args.proxy_port,
        "proxy_username": args.proxy_username,
        "proxy_password": args.proxy_password,
        "proxy_remote_dns": args.proxy_remote_dns,
        "proxy_local_dns": args.proxy_local_dns,
        "no_proxy": args.no_proxy,
    }
    pretty_dump(run_tool("setup_account", payload))


def cmd_doctor_account(args: argparse.Namespace) -> None:
    pretty_dump(run_tool("doctor_account", {"config_path": args.config}))


def cmd_test_login(args: argparse.Namespace) -> None:
    pretty_dump(
        run_tool(
            "test_login",
            {
                "account": args.account,
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
            "account": args.account,
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
            "account": args.account,
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
            "account": args.account,
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
            "account": args.account,
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
        "account": args.account,
        "to": args.to,
        "subject": args.subject,
        "body": load_body(args),
        "config_path": args.config,
        "attachments": args.attach or None,
    }
    if args.html_file:
        payload["html_body"] = Path(args.html_file).expanduser().read_text(encoding="utf-8")
    pretty_dump(run_tool("send_email", payload))


def cmd_draft_email(args: argparse.Namespace) -> None:
    result = draft_email(
        subject=args.subject,
        body=load_body(args),
        tone=args.tone,
        to_name=args.to_name,
        sender_name=args.sender_name,
        output=args.output,
    )
    if result["output_path"]:
        print(f"draft_saved: {result['output_path']}")
        return
    print(result["draft"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone CLI for email-client-skill.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate_parser = subparsers.add_parser("migrate_config")
    migrate_parser.set_defaults(func=cmd_migrate_config)

    setup_parser = subparsers.add_parser("setup_account")
    setup_parser.add_argument("--account", required=True)
    setup_parser.add_argument("--provider", required=True)
    setup_parser.add_argument("--email", required=True)
    setup_parser.add_argument("--login-user")
    setup_parser.add_argument("--display-name")
    setup_parser.add_argument("--auth-mode")
    setup_parser.add_argument("--auth-secret")
    setup_parser.add_argument("--imap-host")
    setup_parser.add_argument("--imap-port", type=int)
    setup_parser.add_argument("--imap-no-ssl", action="store_true")
    setup_parser.add_argument("--imap-starttls", action="store_true")
    setup_parser.add_argument("--smtp-host")
    setup_parser.add_argument("--smtp-port", type=int)
    setup_parser.add_argument("--smtp-no-ssl", action="store_true")
    setup_parser.add_argument("--smtp-starttls", action="store_true")
    setup_parser.add_argument("--proxy-type")
    setup_parser.add_argument("--proxy-host")
    setup_parser.add_argument("--proxy-port", type=int)
    setup_parser.add_argument("--proxy-username")
    setup_parser.add_argument("--proxy-password")
    setup_parser.add_argument("--proxy-remote-dns", action="store_true")
    setup_parser.add_argument("--proxy-local-dns", action="store_true")
    setup_parser.add_argument("--no-proxy", action="store_true")
    setup_parser.set_defaults(func=cmd_setup_account)

    doctor_parser = subparsers.add_parser("doctor_account")
    doctor_parser.set_defaults(func=cmd_doctor_account)

    test_parser = subparsers.add_parser("test_login")
    test_parser.add_argument("--account", required=True)
    test_parser.add_argument("--imap-only", action="store_true")
    test_parser.add_argument("--smtp-only", action="store_true")
    test_parser.set_defaults(func=cmd_test_login)

    list_parser = subparsers.add_parser("list_messages")
    list_parser.add_argument("--account", required=True)
    list_parser.add_argument("--folder", default="INBOX")
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.set_defaults(func=cmd_list_messages)

    search_parser = subparsers.add_parser("search_messages")
    search_parser.add_argument("--account", required=True)
    search_parser.add_argument("--query", default="")
    search_parser.add_argument("--folder", default="INBOX")
    search_parser.add_argument("--scan", type=int, default=200)
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.set_defaults(func=cmd_search_messages)

    get_parser = subparsers.add_parser("get_message")
    get_parser.add_argument("--account", required=True)
    get_parser.add_argument("--uid", required=True)
    get_parser.add_argument("--folder", default="INBOX")
    get_parser.set_defaults(func=cmd_get_message)

    download_parser = subparsers.add_parser("download_attachments")
    download_parser.add_argument("--account", required=True)
    download_parser.add_argument("--uid", required=True)
    download_parser.add_argument("--folder", default="INBOX")
    download_parser.add_argument("--mode", choices=["temp", "archive"], default="temp")
    download_parser.set_defaults(func=cmd_download_attachments)

    send_parser = subparsers.add_parser("send_email")
    send_parser.add_argument("--account", required=True)
    send_parser.add_argument("--to", nargs="+", required=True)
    send_parser.add_argument("--subject", required=True)
    send_parser.add_argument("--body")
    send_parser.add_argument("--body-file")
    send_parser.add_argument("--html-file")
    send_parser.add_argument("--attach", action="append")
    send_parser.set_defaults(func=cmd_send_email)

    draft_parser = subparsers.add_parser("draft_email")
    draft_parser.add_argument("--subject", required=True)
    draft_parser.add_argument("--body")
    draft_parser.add_argument("--body-file")
    draft_parser.add_argument("--tone", choices=["colleague", "formal", "support"], default="colleague")
    draft_parser.add_argument("--to-name", default="")
    draft_parser.add_argument("--sender-name", default="")
    draft_parser.add_argument("--output")
    draft_parser.set_defaults(func=cmd_draft_email)

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
