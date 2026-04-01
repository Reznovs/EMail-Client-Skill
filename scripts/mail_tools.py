#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from mail_core import (
    DEFAULT_CONFIG,
    EmailClientError,
    doctor_account,
    download_attachments,
    draft_email,
    get_message,
    list_messages,
    migrate_config,
    search_messages,
    send_email_tool,
    setup_account,
    test_login,
)


TOOL_MAP: dict[str, Callable[..., dict[str, Any]]] = {
    "migrate_config": migrate_config,
    "setup_account": setup_account,
    "doctor_account": doctor_account,
    "test_login": test_login,
    "list_messages": list_messages,
    "search_messages": search_messages,
    "get_message": get_message,
    "download_attachments": download_attachments,
    "send_email": send_email_tool,
    "draft_email": draft_email,
}


def run_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    handler = TOOL_MAP.get(tool_name)
    if handler is None:
        raise EmailClientError(f"unknown tool: {tool_name}", code="unknown_tool")
    return handler(**payload)


def load_payload(input_json: str | None, input_file: str | None) -> dict[str, Any]:
    if input_json and input_file:
        raise EmailClientError("use either --input-json or --input-file", code="invalid_request")
    if input_file:
        raw = Path(input_file).expanduser().read_text(encoding="utf-8")
    elif input_json:
        raw = input_json
    else:
        raw = "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EmailClientError(f"invalid input json: {exc}", code="invalid_request") from exc
    if not isinstance(payload, dict):
        raise EmailClientError("tool input must be a JSON object", code="invalid_request")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Local JSON tool-call entrypoint for email-client-skill.")
    parser.add_argument("tool_name", choices=sorted(TOOL_MAP.keys()))
    parser.add_argument("--input-json")
    parser.add_argument("--input-file")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--show-default-config", action="store_true")
    args = parser.parse_args()

    if args.show_default_config:
        print(str(DEFAULT_CONFIG))
        return

    try:
        payload = load_payload(args.input_json, args.input_file)
        result = run_tool(args.tool_name, payload)
        dump = json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None)
        print(dump)
    except EmailClientError as exc:
        failure: dict[str, Any] = {
            "status": "error",
            "error_code": exc.code,
            "message": exc.message,
        }
        if exc.details:
            failure["details"] = exc.details
        print(json.dumps(failure, ensure_ascii=False, indent=2 if args.pretty else None))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
