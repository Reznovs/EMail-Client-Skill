#!/usr/bin/env python3

from __future__ import annotations

import base64
import html
import imaplib
import json
import os
import re
import shutil
import smtplib
import socket
import ssl
import tempfile
from dataclasses import dataclass
from datetime import date
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from pathlib import Path
from typing import Any


CONFIG_VERSION = 2
DEFAULT_FOLDER = "INBOX"
DEFAULT_LIMIT = 20
DEFAULT_SCAN = 200
CONNECT_TIMEOUT = float(os.environ.get("CODEX_MAIL_CONNECT_TIMEOUT", "15"))
TEMP_DOWNLOAD_PREFIX = "codex-mail-"
APPROVED_ATTACHMENTS_FILE = ".codex-mail-attachments.json"
DEFAULT_CONFIG = Path(
    os.environ.get("CODEX_MAIL_ACCOUNTS", "~/.config/codex-mail/accounts.json")
).expanduser()
KEYRING_SERVICE = "codex-mail"

try:
    import keyring

    KEYRING_AVAILABLE = True
except ImportError:
    keyring = None
    KEYRING_AVAILABLE = False


@dataclass
class ServerConfig:
    host: str
    port: int
    security: str = "ssl"

    @property
    def uses_ssl(self) -> bool:
        return self.security == "ssl"

    @property
    def uses_starttls(self) -> bool:
        return self.security == "starttls"


@dataclass
class ProxyConfig:
    type: str
    host: str
    port: int
    username: str = ""
    password: str = ""
    remote_dns: bool = True


@dataclass
class IdentityConfig:
    email: str
    login_user: str
    display_name: str


@dataclass
class AuthConfig:
    mode: str
    storage: str
    secret: str | None
    keyring_key: str | None = None


@dataclass
class AccountConfig:
    name: str
    provider: str
    identity: IdentityConfig
    auth: AuthConfig
    imap: ServerConfig
    smtp: ServerConfig
    proxy: ProxyConfig | None = None

    @property
    def email(self) -> str:
        return self.identity.email

    @property
    def login_user(self) -> str:
        return self.identity.login_user

    @property
    def display_name(self) -> str:
        return self.identity.display_name


class EmailClientError(Exception):
    def __init__(self, message: str, *, code: str = "invalid_request", details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}


PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "gmail": {
        "auth_mode": "app_password",
        "imap": ServerConfig(host="imap.gmail.com", port=993, security="ssl"),
        "smtp": ServerConfig(host="smtp.gmail.com", port=465, security="ssl"),
    },
    "qq": {
        "auth_mode": "auth_code",
        "imap": ServerConfig(host="imap.qq.com", port=993, security="ssl"),
        "smtp": ServerConfig(host="smtp.qq.com", port=465, security="ssl"),
    },
}


def auth_secret_placeholder(auth_mode: str) -> str:
    if auth_mode == "app_password":
        return "<app-password>"
    if auth_mode == "auth_code":
        return "<auth-code>"
    return "<password-or-token>"


def is_placeholder_secret(value: str | None) -> bool:
    secret = (value or "").strip()
    return not secret or (secret.startswith("<") and secret.endswith(">"))


def resolve_config_path(config: str | Path | None = None) -> Path:
    if config is None:
        return DEFAULT_CONFIG
    if isinstance(config, Path):
        return config.expanduser()
    return Path(config).expanduser()


def render_config(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise EmailClientError(
            f"account config not found: {path}",
            code="config_not_found",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EmailClientError(
            f"failed to parse config file {path}: {exc}",
            code="invalid_config",
        ) from exc
    if not isinstance(data, dict):
        raise EmailClientError("config file root must be a JSON object", code="invalid_config")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        os.chmod(temp_path, 0o600)
        handle.write(render_config(data))
    os.replace(temp_path, path)
    os.chmod(path, 0o600)


def _blank_v2() -> dict[str, Any]:
    return {"version": CONFIG_VERSION, "accounts": {}}


def security_from_flags(*, ssl_enabled: bool = True, starttls: bool = False) -> str:
    if ssl_enabled:
        return "ssl"
    if starttls:
        return "starttls"
    return "plain"


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def secret_keyring_name(account_name: str) -> str:
    return f"account:{account_name}"


def store_secret_secure(account_name: str, secret: str) -> bool:
    if not KEYRING_AVAILABLE:
        return False
    try:
        keyring.set_password(KEYRING_SERVICE, secret_keyring_name(account_name), secret)
        return True
    except Exception:
        return False


def retrieve_secret_secure(keyring_key: str) -> str | None:
    if not KEYRING_AVAILABLE:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, keyring_key)
    except Exception:
        return None


def delete_secret_secure(keyring_key: str) -> bool:
    if not KEYRING_AVAILABLE:
        return False
    try:
        keyring.delete_password(KEYRING_SERVICE, keyring_key)
        return True
    except Exception:
        return False


def _server_from_raw(raw: Any, *, fallback: ServerConfig | None = None) -> ServerConfig | None:
    if not isinstance(raw, dict):
        return fallback
    host = str(raw.get("host") or "").strip()
    port = raw.get("port")
    security = str(raw.get("security") or "").strip().lower()
    if not host or port in (None, "") or security not in {"ssl", "starttls", "plain"}:
        return fallback
    return ServerConfig(host=host, port=int(port), security=security)


def _proxy_from_raw(raw: Any) -> ProxyConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise EmailClientError("proxy configuration must be a JSON object", code="invalid_config")
    proxy_type = str(raw.get("type") or "").strip().lower()
    host = str(raw.get("host") or "").strip()
    port = raw.get("port")
    if proxy_type not in {"socks5", "http_connect"}:
        raise EmailClientError("proxy.type must be socks5 or http_connect", code="invalid_config")
    if not host or port in (None, ""):
        raise EmailClientError("proxy.host and proxy.port are required", code="invalid_config")
    return ProxyConfig(
        type=proxy_type,
        host=host,
        port=int(port),
        username=str(raw.get("username") or ""),
        password=str(raw.get("password") or ""),
        remote_dns=bool(raw.get("remote_dns", True)),
    )


def _account_from_v2(name: str, raw: Any) -> AccountConfig:
    if not isinstance(raw, dict):
        raise EmailClientError(f"account {name} must be a JSON object", code="invalid_config")
    identity_raw = raw.get("identity")
    auth_raw = raw.get("auth")
    servers_raw = raw.get("servers")
    if not isinstance(identity_raw, dict):
        raise EmailClientError(f"account {name} is missing identity", code="invalid_config")
    if not isinstance(auth_raw, dict):
        raise EmailClientError(f"account {name} is missing auth", code="invalid_config")
    if not isinstance(servers_raw, dict):
        raise EmailClientError(f"account {name} is missing servers", code="invalid_config")

    identity = IdentityConfig(
        email=str(identity_raw.get("email") or "").strip(),
        login_user=str(identity_raw.get("login_user") or "").strip(),
        display_name=str(identity_raw.get("display_name") or "").strip(),
    )
    auth = AuthConfig(
        mode=str(auth_raw.get("mode") or "password").strip(),
        storage=str(auth_raw.get("storage") or "config_file").strip(),
        secret=str(auth_raw.get("secret") or "").strip() or None,
        keyring_key=str(auth_raw.get("keyring_key") or "").strip() or None,
    )
    if auth.storage not in {"config_file", "keyring"}:
        raise EmailClientError(f"account {name} has invalid auth.storage", code="invalid_config")
    if auth.storage == "keyring":
        if not auth.keyring_key:
            raise EmailClientError(f"account {name} is missing auth.keyring_key", code="invalid_config")
        secret = retrieve_secret_secure(auth.keyring_key)
        if not secret:
            raise EmailClientError(
                f"account {name}: credential stored in keyring but keyring access failed",
                code="keyring_unavailable",
            )
        auth = AuthConfig(mode=auth.mode, storage=auth.storage, secret=secret, keyring_key=auth.keyring_key)
    elif not auth.secret:
        raise EmailClientError(f"account {name} is missing auth.secret", code="invalid_config")

    if not identity.email or not identity.login_user or not identity.display_name:
        raise EmailClientError(f"account {name} has incomplete identity fields", code="invalid_config")

    imap = _server_from_raw(servers_raw.get("imap"))
    smtp = _server_from_raw(servers_raw.get("smtp"))
    if imap is None or smtp is None:
        raise EmailClientError(f"account {name} has incomplete server settings", code="invalid_config")

    return AccountConfig(
        name=name,
        provider=str(raw.get("provider") or "custom").strip().lower(),
        identity=identity,
        auth=auth,
        imap=imap,
        smtp=smtp,
        proxy=_proxy_from_raw(raw.get("proxy")),
    )


def serialize_account(account: AccountConfig) -> dict[str, Any]:
    auth_secret = account.auth.secret if account.auth.storage == "config_file" else None
    return {
        "provider": account.provider,
        "identity": {
            "email": account.email,
            "login_user": account.login_user,
            "display_name": account.display_name,
        },
        "auth": {
            "mode": account.auth.mode,
            "storage": account.auth.storage,
            "secret": auth_secret,
            "keyring_key": account.auth.keyring_key,
        },
        "servers": {
            "imap": {
                "host": account.imap.host,
                "port": account.imap.port,
                "security": account.imap.security,
            },
            "smtp": {
                "host": account.smtp.host,
                "port": account.smtp.port,
                "security": account.smtp.security,
            },
        },
        "proxy": (
            {
                "type": account.proxy.type,
                "host": account.proxy.host,
                "port": account.proxy.port,
                "username": account.proxy.username or None,
                "password": account.proxy.password or None,
                "remote_dns": account.proxy.remote_dns,
            }
            if account.proxy
            else None
        ),
    }


def load_v2_document(config_path: str | Path | None = None) -> dict[str, Any]:
    path = resolve_config_path(config_path)
    raw = _load_json(path)
    if raw.get("version") != CONFIG_VERSION:
        raise EmailClientError("config file is still using schema v1", code="migration_required")
    accounts = raw.get("accounts")
    if not isinstance(accounts, dict):
        raise EmailClientError("config file must include an accounts object", code="invalid_config")
    return raw


def load_v2_for_update(config_path: str | Path | None = None) -> dict[str, Any]:
    path = resolve_config_path(config_path)
    if not path.exists():
        return _blank_v2()
    return load_v2_document(path)


def load_account(name: str, config_path: str | Path | None = None) -> AccountConfig:
    raw = load_v2_document(config_path)
    account_raw = raw["accounts"].get(name)
    if account_raw is None:
        raise EmailClientError(f"account not found: {name}", code="account_not_found")
    return _account_from_v2(name, account_raw)


def read_config_version(config_path: str | Path | None = None) -> int | None:
    path = resolve_config_path(config_path)
    if not path.exists():
        return None
    raw = _load_json(path)
    version = raw.get("version")
    return int(version) if isinstance(version, int) else 1


def migrate_config(config_path: str | Path | None = None) -> dict[str, Any]:
    path = resolve_config_path(config_path)
    raw = _load_json(path)
    version = raw.get("version")
    if version == CONFIG_VERSION:
        return {
            "config": str(path),
            "backup_written": "",
            "migration_status": "already_current",
            "config_version": CONFIG_VERSION,
            "status": "ok",
        }
    accounts_raw = raw.get("accounts")
    if not isinstance(accounts_raw, list):
        raise EmailClientError("v1 config file must include an accounts array", code="invalid_config")

    migrated = _blank_v2()
    migrated_accounts: dict[str, Any] = {}
    for index, item in enumerate(accounts_raw, start=1):
        if not isinstance(item, dict):
            raise EmailClientError(f"account entry {index} must be a JSON object", code="invalid_config")
        name = str(item.get("name") or "").strip()
        if not name:
            raise EmailClientError(f"account entry {index} is missing name", code="invalid_config")
        provider = str(item.get("provider") or "custom").strip().lower()
        preset = PROVIDER_PRESETS.get(provider, {})
        merged = deep_merge(
            {
                "auth_mode": str(preset.get("auth_mode") or "password"),
                "imap": (
                    {
                        "host": preset["imap"].host,
                        "port": preset["imap"].port,
                        "security": preset["imap"].security,
                    }
                    if preset.get("imap")
                    else {}
                ),
                "smtp": (
                    {
                        "host": preset["smtp"].host,
                        "port": preset["smtp"].port,
                        "security": preset["smtp"].security,
                    }
                    if preset.get("smtp")
                    else {}
                ),
            },
            item,
        )
        auth_mode = str(merged.get("auth_mode") or preset.get("auth_mode") or "password").strip()
        auth_secret = str(merged.get("auth_secret") or "").strip()
        if auth_secret == "<stored-in-keyring>":
            auth_storage = "keyring"
            auth_value = None
            keyring_key = secret_keyring_name(name)
        else:
            auth_storage = "config_file"
            auth_value = auth_secret or auth_secret_placeholder(auth_mode)
            keyring_key = None

        imap_raw = merged.get("imap") or {}
        smtp_raw = merged.get("smtp") or {}
        imap_host = str(imap_raw.get("host") or "").strip()
        smtp_host = str(smtp_raw.get("host") or "").strip()
        imap_port = imap_raw.get("port")
        smtp_port = smtp_raw.get("port")
        if not imap_host or imap_port in (None, ""):
            raise EmailClientError(f"account {name} is missing imap host/port during migration", code="invalid_config")
        if not smtp_host or smtp_port in (None, ""):
            raise EmailClientError(f"account {name} is missing smtp host/port during migration", code="invalid_config")

        migrated_accounts[name] = {
            "provider": provider,
            "identity": {
                "email": str(merged.get("email") or "").strip(),
                "login_user": str(merged.get("login_user") or merged.get("email") or "").strip(),
                "display_name": str(merged.get("display_name") or merged.get("email") or "").strip(),
            },
            "auth": {
                "mode": auth_mode,
                "storage": auth_storage,
                "secret": auth_value,
                "keyring_key": keyring_key,
            },
            "servers": {
                "imap": {
                    "host": imap_host,
                    "port": int(imap_port),
                    "security": str(imap_raw.get("security") or "").strip().lower()
                    or security_from_flags(
                        ssl_enabled=bool(imap_raw.get("ssl", True)),
                        starttls=bool(imap_raw.get("starttls", False)),
                    ),
                },
                "smtp": {
                    "host": smtp_host,
                    "port": int(smtp_port),
                    "security": str(smtp_raw.get("security") or "").strip().lower()
                    or security_from_flags(
                        ssl_enabled=bool(smtp_raw.get("ssl", True)),
                        starttls=bool(smtp_raw.get("starttls", False)),
                    ),
                },
            },
            "proxy": item.get("proxy"),
        }
    migrated["accounts"] = migrated_accounts

    backup_path = path.with_name(path.name + ".v1.bak")
    shutil.copy2(path, backup_path)
    _write_json(path, migrated)
    return {
        "config": str(path),
        "backup_written": str(backup_path),
        "migration_status": "migrated",
        "config_version": CONFIG_VERSION,
        "account_count": len(migrated_accounts),
        "status": "ok",
    }


def doctor_account(config_path: str | Path | None = None) -> dict[str, Any]:
    path = resolve_config_path(config_path)
    result: dict[str, Any] = {"config": str(path), "accounts": []}
    if not path.exists():
        result["doctor_status"] = "needs_attention"
        result["issues"] = ["account config does not exist"]
        result["next_step"] = "run setup_account to create a v2 config."
        return result

    raw = _load_json(path)
    version = raw.get("version")
    if version != CONFIG_VERSION:
        result["doctor_status"] = "needs_attention"
        result["config_version"] = 1
        result["migration_required"] = True
        result["issues"] = ["config file is still using schema v1"]
        result["next_step"] = "run migrate_config before using mailbox operations."
        return result

    accounts = raw.get("accounts")
    if not isinstance(accounts, dict):
        raise EmailClientError("config file must include an accounts object", code="invalid_config")

    any_issues = False
    for name, account_raw in accounts.items():
        issues: list[str] = []
        notes: list[str] = []
        try:
            account = _account_from_v2(name, account_raw)
            if account.auth.storage == "config_file" and is_placeholder_secret(account.auth.secret):
                issues.append("auth.secret is missing or still uses a placeholder")
            notes.append(f"auth_mode: {account.auth.mode}")
            notes.append(f"secret_storage: {account.auth.storage}")
            if account.proxy:
                notes.append(
                    f"proxy: {account.proxy.type}://{account.proxy.host}:{account.proxy.port} (remote_dns={account.proxy.remote_dns})"
                )
        except EmailClientError as exc:
            issues.append(exc.message)
        result["accounts"].append(
            {
                "name": name,
                "status": "needs_attention" if issues else "ok",
                "issues": issues,
                "notes": notes,
            }
        )
        if issues:
            any_issues = True
    result["config_version"] = CONFIG_VERSION
    result["account_count"] = len(accounts)
    result["doctor_status"] = "needs_attention" if any_issues else "ok"
    return result


def _validate_account_name(name: str) -> str:
    if any(sep in name for sep in ("/", "\\")):
        raise EmailClientError("account must not contain path separators", code="invalid_setup")
    if name in {".", ".."} or ".." in name:
        raise EmailClientError("account must not contain path traversal segments", code="invalid_setup")
    if name.startswith("."):
        raise EmailClientError("account must not start with a dot", code="invalid_setup")
    return name


def _merge_server(
    base: ServerConfig | None,
    *,
    host: str | None,
    port: int | None,
    disable_ssl: bool,
    starttls: bool,
    required_name: str,
) -> ServerConfig:
    default_security = base.security if base else "ssl"
    security = default_security
    if disable_ssl and starttls:
        security = "starttls"
    elif disable_ssl:
        security = "plain"
    elif starttls:
        security = "starttls"

    final_host = (host or (base.host if base else "")).strip()
    final_port = port if port is not None else (base.port if base else None)
    if not final_host or final_port in (None, ""):
        raise EmailClientError(f"{required_name} host and port are required", code="invalid_setup")
    return ServerConfig(host=final_host, port=int(final_port), security=security)


def _merge_proxy(
    existing_raw: Any,
    *,
    proxy_type: str | None,
    proxy_host: str | None,
    proxy_port: int | None,
    proxy_username: str | None,
    proxy_password: str | None,
    proxy_remote_dns: bool,
    proxy_local_dns: bool,
    no_proxy: bool,
) -> ProxyConfig | None:
    if no_proxy:
        return None
    current = _proxy_from_raw(existing_raw)
    has_proxy_args = any(
        [
            proxy_type,
            proxy_host,
            proxy_port is not None,
            proxy_username is not None,
            proxy_password is not None,
            proxy_remote_dns,
            proxy_local_dns,
        ]
    )
    if not has_proxy_args:
        return current

    final_type = str(proxy_type or (current.type if current else "")).strip().lower()
    final_host = str(proxy_host or (current.host if current else "")).strip()
    final_port = proxy_port if proxy_port is not None else (current.port if current else None)
    final_username = proxy_username if proxy_username is not None else (current.username if current else "")
    final_password = proxy_password if proxy_password is not None else (current.password if current else "")
    remote_dns = current.remote_dns if current else True
    if proxy_remote_dns:
        remote_dns = True
    if proxy_local_dns:
        remote_dns = False
    if final_type not in {"socks5", "http_connect"}:
        raise EmailClientError("proxy_type must be socks5 or http_connect", code="invalid_setup")
    if not final_host or final_port in (None, ""):
        raise EmailClientError("proxy_host and proxy_port are required when proxy is enabled", code="invalid_setup")
    return ProxyConfig(
        type=final_type,
        host=final_host,
        port=int(final_port),
        username=final_username or "",
        password=final_password or "",
        remote_dns=remote_dns,
    )


def setup_account(
    *,
    account: str,
    provider: str,
    email: str,
    config_path: str | Path | None = None,
    login_user: str | None = None,
    display_name: str | None = None,
    auth_mode: str | None = None,
    auth_secret: str | None = None,
    imap_host: str | None = None,
    imap_port: int | None = None,
    imap_no_ssl: bool = False,
    imap_starttls: bool = False,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
    smtp_no_ssl: bool = False,
    smtp_starttls: bool = False,
    proxy_type: str | None = None,
    proxy_host: str | None = None,
    proxy_port: int | None = None,
    proxy_username: str | None = None,
    proxy_password: str | None = None,
    proxy_remote_dns: bool = False,
    proxy_local_dns: bool = False,
    no_proxy: bool = False,
) -> dict[str, Any]:
    name = _validate_account_name(account.strip())
    provider_name = provider.strip().lower()
    mailbox_email = email.strip()
    if not name:
        raise EmailClientError("account is required", code="invalid_setup")
    if not provider_name:
        raise EmailClientError("provider is required", code="invalid_setup")
    if not mailbox_email:
        raise EmailClientError("email is required", code="invalid_setup")

    config = resolve_config_path(config_path)
    document = load_v2_for_update(config)
    existing_raw = document["accounts"].get(name)
    existing_provider = str(existing_raw.get("provider") or "").strip().lower() if isinstance(existing_raw, dict) else ""
    existing_identity = existing_raw.get("identity") if isinstance(existing_raw, dict) else {}
    existing_auth = existing_raw.get("auth") if isinstance(existing_raw, dict) else {}
    existing_servers = existing_raw.get("servers") if isinstance(existing_raw, dict) else {}
    provider_changed = bool(existing_provider and existing_provider != provider_name)
    email_changed = bool(
        isinstance(existing_identity, dict)
        and str(existing_identity.get("email") or "").strip()
        and str(existing_identity.get("email") or "").strip() != mailbox_email
    )
    preserve_defaults = bool(existing_raw) and not provider_changed and not email_changed

    preset = PROVIDER_PRESETS.get(provider_name, {})
    if preserve_defaults:
        base_imap = _server_from_raw(
            existing_servers.get("imap") if isinstance(existing_servers, dict) else None,
            fallback=preset.get("imap"),
        )
        base_smtp = _server_from_raw(
            existing_servers.get("smtp") if isinstance(existing_servers, dict) else None,
            fallback=preset.get("smtp"),
        )
    else:
        base_imap = preset.get("imap")
        base_smtp = preset.get("smtp")

    final_auth_mode = (
        auth_mode
        or (existing_auth.get("mode") if preserve_defaults and isinstance(existing_auth, dict) else None)
        or preset.get("auth_mode")
        or "password"
    ).strip()
    final_login_user = (
        login_user
        or (existing_identity.get("login_user") if preserve_defaults and isinstance(existing_identity, dict) else None)
        or mailbox_email
    ).strip()
    final_display_name = (
        display_name
        or (existing_identity.get("display_name") if preserve_defaults and isinstance(existing_identity, dict) else None)
        or mailbox_email
    ).strip()

    previous_storage = str(existing_auth.get("storage") or "config_file") if isinstance(existing_auth, dict) else "config_file"
    previous_keyring_key = str(existing_auth.get("keyring_key") or "").strip() if isinstance(existing_auth, dict) else ""
    previous_secret = str(existing_auth.get("secret") or "").strip() if isinstance(existing_auth, dict) else ""
    secret_status = "provided"
    secret_storage = previous_storage
    keyring_key = previous_keyring_key or secret_keyring_name(name)
    secret_value: str | None = None

    if auth_secret is not None:
        candidate = auth_secret.strip()
        if is_placeholder_secret(candidate):
            secret_status = "placeholder"
            secret_storage = "config_file"
            secret_value = auth_secret_placeholder(final_auth_mode)
            if previous_storage == "keyring" and previous_keyring_key:
                delete_secret_secure(previous_keyring_key)
                keyring_key = None
        else:
            if store_secret_secure(name, candidate):
                secret_storage = "keyring"
                secret_value = candidate
                keyring_key = secret_keyring_name(name)
                if previous_storage == "keyring" and previous_keyring_key and previous_keyring_key != keyring_key:
                    delete_secret_secure(previous_keyring_key)
            else:
                secret_storage = "config_file"
                secret_value = candidate
                keyring_key = None
                if previous_storage == "keyring" and previous_keyring_key:
                    delete_secret_secure(previous_keyring_key)
    elif existing_raw:
        if previous_storage == "keyring":
            secret_storage = "keyring"
            keyring_key = previous_keyring_key or secret_keyring_name(name)
            secret_value = None
        else:
            secret_storage = "config_file"
            secret_value = previous_secret or auth_secret_placeholder(final_auth_mode)
            secret_status = "placeholder" if is_placeholder_secret(secret_value) else "provided"
            keyring_key = None
    else:
        secret_storage = "config_file"
        secret_status = "placeholder"
        secret_value = auth_secret_placeholder(final_auth_mode)
        keyring_key = None

    if provider_name in PROVIDER_PRESETS:
        final_imap = _merge_server(
            base_imap or preset.get("imap"),
            host=imap_host,
            port=imap_port,
            disable_ssl=imap_no_ssl,
            starttls=imap_starttls,
            required_name="imap",
        )
        final_smtp = _merge_server(
            base_smtp or preset.get("smtp"),
            host=smtp_host,
            port=smtp_port,
            disable_ssl=smtp_no_ssl,
            starttls=smtp_starttls,
            required_name="smtp",
        )
    else:
        final_imap = _merge_server(
            base_imap,
            host=imap_host,
            port=imap_port,
            disable_ssl=imap_no_ssl,
            starttls=imap_starttls,
            required_name="custom imap",
        )
        final_smtp = _merge_server(
            base_smtp,
            host=smtp_host,
            port=smtp_port,
            disable_ssl=smtp_no_ssl,
            starttls=smtp_starttls,
            required_name="custom smtp",
        )

    final_proxy = _merge_proxy(
        existing_raw.get("proxy") if isinstance(existing_raw, dict) else None,
        proxy_type=proxy_type,
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        proxy_username=proxy_username,
        proxy_password=proxy_password,
        proxy_remote_dns=proxy_remote_dns,
        proxy_local_dns=proxy_local_dns,
        no_proxy=no_proxy,
    )

    account_config = AccountConfig(
        name=name,
        provider=provider_name,
        identity=IdentityConfig(
            email=mailbox_email,
            login_user=final_login_user,
            display_name=final_display_name,
        ),
        auth=AuthConfig(
            mode=final_auth_mode,
            storage=secret_storage,
            secret=secret_value,
            keyring_key=keyring_key if secret_storage == "keyring" else None,
        ),
        imap=final_imap,
        smtp=final_smtp,
        proxy=final_proxy,
    )
    document["accounts"][name] = serialize_account(account_config)
    _write_json(config, document)
    return {
        "status": "ok",
        "account": name,
        "provider": provider_name,
        "config": str(config),
        "config_version": CONFIG_VERSION,
        "secret_status": secret_status,
        "secret_storage": secret_storage,
        "provider_hint": provider_advice(provider_name),
    }


def provider_advice(provider: str) -> str:
    if provider == "gmail":
        return "Use a Gmail app password after enabling 2-Step Verification. Add proxy settings only when your network requires it."
    if provider == "qq":
        return "Enable IMAP/SMTP in QQ Mail settings and generate an auth code."
    return "For custom providers, confirm the IMAP/SMTP host, port, and transport security mode."


def decode_mime_header(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except Exception:
        return raw.strip()


def clean_html_text(raw: str) -> str:
    text = raw
    text = re.sub(r'(?i)\s+on\w+\s*=\s*["\'][^"\']*["\']', "", text)
    text = re.sub(r'(?i)\s+on\w+\s*=\s*[^\s>]+', "", text)
    text = re.sub(r'(?i)javascript\s*:', "", text)
    text = re.sub(r'(?i)data\s*:', "", text)
    text = re.sub(r'(?i)vbscript\s*:', "", text)
    text = re.sub(r"(?is)<(script|style).*?>.*?</\\1>", " ", text)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p\s*>", "\n", text)
    text = re.sub(r"(?is)</div\s*>", "\n", text)
    text = re.sub(r"(?is)</li\s*>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def get_body_text(msg: Message) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_filename():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            content = payload.decode(charset, errors="replace")
        except LookupError:
            content = payload.decode("utf-8", errors="replace")
        if part.get_content_type() == "text/plain":
            if content.strip():
                plain_parts.append(content.strip())
        elif part.get_content_type() == "text/html":
            cleaned = clean_html_text(content)
            if cleaned:
                html_parts.append(cleaned)
    if plain_parts:
        return "\n\n".join(plain_parts).strip()
    if html_parts:
        return "\n\n".join(html_parts).strip()
    return ""


def format_preview(text: str, limit: int = 140) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def safe_filename(name: str, fallback: str, max_length: int = 255) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._\u4e00-\u9fff-]+", "_", name.strip())
    cleaned = cleaned.lstrip(".")
    if len(cleaned) > max_length:
        if "." in cleaned:
            base, ext = cleaned.rsplit(".", 1)
            ext = ext[:20]
            cleaned = base[: max_length - len(ext) - 1] + "." + ext
        else:
            cleaned = cleaned[:max_length]
    return cleaned or fallback


def _attachment_manifest_path(target_dir: Path) -> Path:
    return target_dir / APPROVED_ATTACHMENTS_FILE


def _register_saved_attachments(target_dir: Path, saved: list[Path]) -> None:
    manifest = _attachment_manifest_path(target_dir)
    payload = {
        "version": 1,
        "approved_files": sorted(item.name for item in saved),
    }
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest.chmod(0o600)


def _load_approved_attachment_names(target_dir: Path) -> set[str]:
    manifest = _attachment_manifest_path(target_dir)
    if not manifest.is_file():
        raise EmailClientError("attachment is not in an approved download directory", code="invalid_request")
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EmailClientError("attachment approval manifest is invalid", code="invalid_request") from exc
    approved = payload.get("approved_files")
    if not isinstance(approved, list) or not all(isinstance(item, str) and item for item in approved):
        raise EmailClientError("attachment approval manifest is invalid", code="invalid_request")
    return set(approved)


def _validate_send_attachment(path_value: str) -> Path:
    candidate = Path(path_value).expanduser()
    if candidate.is_symlink():
        raise EmailClientError(f"attachment is not an approved file: {candidate}", code="invalid_request")
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise EmailClientError(f"attachment not found: {candidate}", code="invalid_request") from exc
    if not resolved.is_file():
        raise EmailClientError(f"attachment is not a file: {candidate}", code="invalid_request")
    parent = resolved.parent
    approved = _load_approved_attachment_names(parent)
    try:
        resolved.relative_to(parent.resolve())
    except ValueError as exc:
        raise EmailClientError(f"attachment is outside approved directory: {candidate}", code="invalid_request") from exc
    if resolved.name not in approved:
        raise EmailClientError(f"attachment is not approved for sending: {candidate}", code="invalid_request")
    return resolved


def save_attachments(msg: Message, target_dir: Path) -> list[Path]:
    saved: list[Path] = []
    target_dir.mkdir(parents=True, exist_ok=True)
    target_dir.chmod(0o700)
    used_names: set[str] = set()
    for index, part in enumerate(msg.walk(), start=1):
        filename = part.get_filename()
        if not filename:
            continue
        decoded = decode_mime_header(filename) or f"attachment-{index}"
        final_name = safe_filename(decoded, f"attachment-{index}")
        candidate = final_name
        stem = Path(final_name).stem or "attachment"
        suffix = Path(final_name).suffix
        counter = 2
        while candidate in used_names or (target_dir / candidate).exists():
            candidate = f"{stem}-{counter}{suffix}"
            counter += 1
        used_names.add(candidate)
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        path = target_dir / candidate
        path.write_bytes(payload)
        path.chmod(0o600)
        saved.append(path)
    _register_saved_attachments(target_dir, saved)
    return saved


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise RuntimeError("proxy connection closed unexpectedly")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def create_direct_connection(host: str, port: int, timeout: float) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    return sock


def resolve_proxy_destination(host: str, port: int, remote_dns: bool) -> tuple[int, bytes]:
    if remote_dns:
        encoded = host.encode("idna")
        if len(encoded) > 255:
            raise RuntimeError("proxy destination hostname is too long")
        return 0x03, bytes([len(encoded)]) + encoded
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    address = infos[0][4][0]
    if ":" in address:
        return 0x04, socket.inet_pton(socket.AF_INET6, address)
    return 0x01, socket.inet_aton(address)


def create_socks5_connection(host: str, port: int, proxy: ProxyConfig, timeout: float) -> socket.socket:
    sock = create_direct_connection(proxy.host, proxy.port, timeout)
    methods = [0x00]
    if proxy.username or proxy.password:
        methods.append(0x02)
    sock.sendall(bytes([0x05, len(methods), *methods]))
    greeting = recv_exact(sock, 2)
    if greeting[0] != 0x05:
        raise RuntimeError("invalid SOCKS5 proxy response")
    method = greeting[1]
    if method == 0xFF:
        raise RuntimeError("SOCKS5 proxy rejected all authentication methods")
    if method == 0x02:
        username = proxy.username.encode("utf-8")
        password = proxy.password.encode("utf-8")
        sock.sendall(bytes([0x01, len(username)]) + username + bytes([len(password)]) + password)
        auth_reply = recv_exact(sock, 2)
        if auth_reply[1] != 0x00:
            raise RuntimeError("SOCKS5 proxy authentication failed")
    atyp, address = resolve_proxy_destination(host, port, proxy.remote_dns)
    sock.sendall(b"\x05\x01\x00" + bytes([atyp]) + address + port.to_bytes(2, "big"))
    reply = recv_exact(sock, 4)
    if reply[1] != 0x00:
        raise RuntimeError(f"SOCKS5 proxy connect failed with code {reply[1]}")
    bound_type = reply[3]
    if bound_type == 0x01:
        recv_exact(sock, 4)
    elif bound_type == 0x03:
        recv_exact(sock, recv_exact(sock, 1)[0])
    elif bound_type == 0x04:
        recv_exact(sock, 16)
    recv_exact(sock, 2)
    return sock


def create_http_connect_connection(host: str, port: int, proxy: ProxyConfig, timeout: float) -> socket.socket:
    sock = create_direct_connection(proxy.host, proxy.port, timeout)
    headers = [
        f"CONNECT {host}:{port} HTTP/1.1",
        f"Host: {host}:{port}",
        "Proxy-Connection: Keep-Alive",
    ]
    if proxy.username or proxy.password:
        token = base64.b64encode(f"{proxy.username}:{proxy.password}".encode("utf-8")).decode("ascii")
        headers.append(f"Proxy-Authorization: Basic {token}")
    sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("utf-8"))
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("HTTP proxy closed during CONNECT handshake")
        response += chunk
        if len(response) > 65536:
            raise RuntimeError("HTTP proxy response headers are too large")
    status_line = response.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or parts[1] != "200":
        raise RuntimeError(f"HTTP CONNECT failed: {status_line}")
    return sock


def create_connection(host: str, port: int, proxy: ProxyConfig | None, timeout: float = CONNECT_TIMEOUT) -> socket.socket:
    if proxy is None:
        return create_direct_connection(host, port, timeout)
    if proxy.type == "socks5":
        return create_socks5_connection(host, port, proxy, timeout)
    if proxy.type == "http_connect":
        return create_http_connect_connection(host, port, proxy, timeout)
    raise RuntimeError(f"unsupported proxy type: {proxy.type}")


class ProxyIMAP4(imaplib.IMAP4):
    def __init__(self, host: str, port: int, *, proxy: ProxyConfig, timeout: float) -> None:
        self._proxy = proxy
        self._connect_timeout = timeout
        super().__init__(host, port, timeout)

    def open(self, host: str = "", port: int = imaplib.IMAP4_PORT, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.sock = create_connection(host, port, self._proxy, timeout or self._connect_timeout)
        self.file = self.sock.makefile("rb")


class ProxyIMAP4_SSL(imaplib.IMAP4_SSL):
    def __init__(self, host: str, port: int, *, proxy: ProxyConfig, ssl_context: ssl.SSLContext, timeout: float) -> None:
        self._proxy = proxy
        self._connect_timeout = timeout
        self._ssl_context = ssl_context
        super().__init__(host, port, ssl_context=ssl_context, timeout=timeout)

    def open(self, host: str = "", port: int = imaplib.IMAP4_SSL_PORT, timeout: float | None = None) -> None:
        raw_sock = create_connection(host, port, self._proxy, timeout or self._connect_timeout)
        self.host = host
        self.port = port
        self.sock = self._ssl_context.wrap_socket(raw_sock, server_hostname=host)
        self.sock.settimeout(timeout or self._connect_timeout)
        self.file = self.sock.makefile("rb")


class ProxySMTP(smtplib.SMTP):
    def __init__(
        self,
        host: str = "",
        port: int = 0,
        local_hostname: str | None = None,
        timeout: float = CONNECT_TIMEOUT,
        source_address: tuple[str, int] | None = None,
        *,
        proxy: ProxyConfig,
    ) -> None:
        self._proxy = proxy
        super().__init__(host=host, port=port, local_hostname=local_hostname, timeout=timeout, source_address=source_address)

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        return create_connection(host, port, self._proxy, timeout)


class ProxySMTP_SSL(smtplib.SMTP_SSL):
    def __init__(
        self,
        host: str = "",
        port: int = 0,
        local_hostname: str | None = None,
        timeout: float = CONNECT_TIMEOUT,
        source_address: tuple[str, int] | None = None,
        context: ssl.SSLContext | None = None,
        *,
        proxy: ProxyConfig,
    ) -> None:
        self._proxy = proxy
        super().__init__(
            host=host,
            port=port,
            local_hostname=local_hostname,
            timeout=timeout,
            source_address=source_address,
            context=context,
        )

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        raw_sock = create_connection(host, port, self._proxy, timeout)
        wrapped = self.context.wrap_socket(raw_sock, server_hostname=host)
        wrapped.settimeout(timeout)
        return wrapped


def create_imap_client(account: AccountConfig) -> imaplib.IMAP4 | imaplib.IMAP4_SSL:
    context = ssl.create_default_context()
    if account.imap.uses_ssl:
        if account.proxy:
            return ProxyIMAP4_SSL(
                account.imap.host,
                account.imap.port,
                proxy=account.proxy,
                ssl_context=context,
                timeout=CONNECT_TIMEOUT,
            )
        return imaplib.IMAP4_SSL(account.imap.host, account.imap.port, ssl_context=context, timeout=CONNECT_TIMEOUT)
    if account.proxy:
        client: imaplib.IMAP4 | imaplib.IMAP4_SSL = ProxyIMAP4(
            account.imap.host,
            account.imap.port,
            proxy=account.proxy,
            timeout=CONNECT_TIMEOUT,
        )
    else:
        client = imaplib.IMAP4(account.imap.host, account.imap.port, CONNECT_TIMEOUT)
    if account.imap.uses_starttls:
        client.starttls(ssl_context=context)
    return client


class MailClient:
    def __init__(self, account: AccountConfig) -> None:
        self.account = account
        self.imap: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "MailClient":
        self.imap = create_imap_client(self.account)
        self.imap.login(self.account.login_user, self.account.auth.secret or "")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.imap is None:
            return
        try:
            self.imap.logout()
        except Exception:
            pass

    def _require_imap(self) -> imaplib.IMAP4 | imaplib.IMAP4_SSL:
        if self.imap is None:
            raise RuntimeError("IMAP is not connected")
        return self.imap

    def select_folder(self, folder: str) -> None:
        mailbox = folder
        if " " in mailbox and not (mailbox.startswith('"') and mailbox.endswith('"')):
            mailbox = f'"{mailbox}"'
        status, _ = self._require_imap().select(mailbox, readonly=True)
        if status != "OK":
            raise RuntimeError(f"failed to open mailbox folder: {folder}")

    def search_all_uids(self) -> list[bytes]:
        status, data = self._require_imap().uid("search", None, "ALL")
        if status != "OK" or not data or not data[0]:
            return []
        return data[0].split()

    def fetch_headers(self, uid: bytes) -> dict[str, str]:
        status, data = self._require_imap().uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
        if status != "OK" or not data or not data[0]:
            raise RuntimeError(f"failed to read message headers: {uid.decode()}")
        msg = message_from_bytes(data[0][1])
        return {
            "uid": uid.decode(),
            "subject": decode_mime_header(msg.get("Subject")),
            "from": decode_mime_header(msg.get("From")),
            "date": decode_mime_header(msg.get("Date")),
        }

    def fetch_message(self, uid: bytes) -> Message:
        status, data = self._require_imap().uid("fetch", uid, "(RFC822)")
        if status != "OK" or not data or not data[0]:
            raise RuntimeError(f"failed to read message body: {uid.decode()}")
        return message_from_bytes(data[0][1])


def archive_root() -> Path:
    return Path.home() / "Documents" / "CodexMail" / "attachments"


def build_download_dir(account: AccountConfig, uid: str, mode: str) -> Path:
    if mode == "temp":
        return Path(tempfile.mkdtemp(prefix=TEMP_DOWNLOAD_PREFIX))
    stamp = date.today().isoformat()
    root = archive_root().resolve()
    target = (root / safe_filename(account.name, "account") / stamp / safe_filename(uid, "message")).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise EmailClientError("archive path escaped the attachment root", code="invalid_request") from exc
    return target


def list_message_attachments(msg: Message) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    for index, part in enumerate(msg.walk(), start=1):
        filename = part.get_filename()
        if not filename:
            continue
        decoded = decode_mime_header(filename) or f"attachment-{index}"
        payload = part.get_payload(decode=True)
        attachments.append(
            {
                "filename": safe_filename(decoded, f"attachment-{index}"),
                "original_filename": decoded,
                "content_type": part.get_content_type(),
                "size": len(payload) if payload is not None else 0,
            }
        )
    return attachments


def build_message_detail(uid: str, msg: Message) -> dict[str, Any]:
    return {
        "uid": uid,
        "date": decode_mime_header(msg.get("Date")),
        "from": decode_mime_header(msg.get("From")),
        "to": decode_mime_header(msg.get("To")),
        "cc": decode_mime_header(msg.get("Cc")),
        "subject": decode_mime_header(msg.get("Subject")),
        "body_text": get_body_text(msg),
        "attachments": list_message_attachments(msg),
    }


def normalize_recipients(raw: str | list[str]) -> list[str]:
    if isinstance(raw, str):
        parts = re.split(r"[,;\n]", raw)
    else:
        parts = raw
    recipients = [str(item).strip() for item in parts if str(item).strip()]
    if not recipients:
        raise EmailClientError("at least one recipient is required", code="invalid_request")
    return recipients


def send_email(
    account: AccountConfig,
    *,
    to: str | list[str],
    subject: str,
    body: str,
    html_body: str | None = None,
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    recipients = normalize_recipients(to)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{account.display_name} <{account.email}>"
    msg["To"] = ", ".join(recipients)
    msg.set_content(body, charset="utf-8")

    if html_body:
        msg.add_alternative(html_body, subtype="html", charset="utf-8")

    attached_files: list[str] = []
    for attachment in attachments or []:
        path = _validate_send_attachment(attachment)
        msg.add_attachment(
            path.read_bytes(),
            maintype="application",
            subtype="octet-stream",
            filename=path.name,
        )
        attached_files.append(str(path))

    context = ssl.create_default_context()
    if account.smtp.uses_ssl:
        server_cls = ProxySMTP_SSL if account.proxy else smtplib.SMTP_SSL
        kwargs: dict[str, Any] = {"context": context, "timeout": CONNECT_TIMEOUT}
        if account.proxy:
            kwargs["proxy"] = account.proxy
        with server_cls(account.smtp.host, account.smtp.port, **kwargs) as server:
            server.login(account.login_user, account.auth.secret or "")
            server.send_message(msg)
    else:
        server_cls = ProxySMTP if account.proxy else smtplib.SMTP
        kwargs: dict[str, Any] = {"timeout": CONNECT_TIMEOUT}
        if account.proxy:
            kwargs["proxy"] = account.proxy
        with server_cls(account.smtp.host, account.smtp.port, **kwargs) as server:
            server.ehlo()
            if account.smtp.uses_starttls:
                server.starttls(context=context)
                server.ehlo()
            server.login(account.login_user, account.auth.secret or "")
            server.send_message(msg)

    return {
        "account": account.name,
        "to": recipients,
        "subject": subject,
        "attachments": attached_files,
        "status": "sent",
    }


def test_imap_login(account: AccountConfig) -> None:
    client = create_imap_client(account)
    try:
        client.login(account.login_user, account.auth.secret or "")
    finally:
        try:
            client.logout()
        except Exception:
            pass


def test_smtp_login(account: AccountConfig) -> None:
    context = ssl.create_default_context()
    if account.smtp.uses_ssl:
        server_cls = ProxySMTP_SSL if account.proxy else smtplib.SMTP_SSL
        kwargs: dict[str, Any] = {"context": context, "timeout": CONNECT_TIMEOUT}
        if account.proxy:
            kwargs["proxy"] = account.proxy
        with server_cls(account.smtp.host, account.smtp.port, **kwargs) as server:
            server.login(account.login_user, account.auth.secret or "")
        return

    server_cls = ProxySMTP if account.proxy else smtplib.SMTP
    kwargs: dict[str, Any] = {"timeout": CONNECT_TIMEOUT}
    if account.proxy:
        kwargs["proxy"] = account.proxy
    with server_cls(account.smtp.host, account.smtp.port, **kwargs) as server:
        server.ehlo()
        if account.smtp.uses_starttls:
            server.starttls(context=context)
            server.ehlo()
        server.login(account.login_user, account.auth.secret or "")


def list_messages(
    *,
    account: str,
    config_path: str | Path | None = None,
    folder: str = DEFAULT_FOLDER,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    mailbox = load_account(account, config_path)
    messages: list[dict[str, Any]] = []
    with MailClient(mailbox) as client:
        client.select_folder(folder)
        uids = client.search_all_uids()
        for uid in list(reversed(uids[-limit:])):
            header = client.fetch_headers(uid)
            messages.append(header)
    return {"status": "ok", "account": mailbox.name, "folder": folder, "messages": messages}


def search_messages(
    *,
    account: str,
    query: str = "",
    config_path: str | Path | None = None,
    folder: str = DEFAULT_FOLDER,
    scan: int = DEFAULT_SCAN,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    mailbox = load_account(account, config_path)
    keyword = query.lower()
    messages: list[dict[str, Any]] = []
    with MailClient(mailbox) as client:
        client.select_folder(folder)
        uids = client.search_all_uids()
        scanned = list(reversed(uids[-scan:]))
        matched = 0
        for uid in scanned:
            header = client.fetch_headers(uid)
            haystack = " ".join([header["subject"], header["from"], header["date"]]).lower()
            if keyword and keyword not in haystack:
                msg = client.fetch_message(uid)
                body = get_body_text(msg).lower()
                if keyword not in body:
                    continue
                preview = format_preview(body)
            else:
                msg = client.fetch_message(uid)
                preview = format_preview(get_body_text(msg))
            messages.append({**header, "preview": preview})
            matched += 1
            if matched >= limit:
                break
    return {"status": "ok", "account": mailbox.name, "folder": folder, "query": query, "messages": messages}


def get_message(
    *,
    account: str,
    uid: str,
    config_path: str | Path | None = None,
    folder: str = DEFAULT_FOLDER,
) -> dict[str, Any]:
    mailbox = load_account(account, config_path)
    with MailClient(mailbox) as client:
        client.select_folder(folder)
        msg = client.fetch_message(uid.encode())
    return {"status": "ok", "account": mailbox.name, "folder": folder, "message": build_message_detail(uid, msg)}


def download_attachments(
    *,
    account: str,
    uid: str,
    mode: str = "temp",
    config_path: str | Path | None = None,
    folder: str = DEFAULT_FOLDER,
) -> dict[str, Any]:
    if mode not in {"temp", "archive"}:
        raise EmailClientError("mode must be temp or archive", code="invalid_request")
    mailbox = load_account(account, config_path)
    with MailClient(mailbox) as client:
        client.select_folder(folder)
        msg = client.fetch_message(uid.encode())
        target_dir = build_download_dir(mailbox, uid, mode)
        saved = save_attachments(msg, target_dir)
    return {
        "status": "ok",
        "account": mailbox.name,
        "uid": uid,
        "mode": mode,
        "target_dir": str(target_dir),
        "files": [str(item) for item in saved],
    }


def send_email_tool(
    *,
    account: str,
    to: str | list[str],
    subject: str,
    body: str,
    config_path: str | Path | None = None,
    html_body: str | None = None,
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    mailbox = load_account(account, config_path)
    return send_email(
        mailbox,
        to=to,
        subject=subject,
        body=body,
        html_body=html_body,
        attachments=attachments,
    )


def test_login(
    *,
    account: str,
    config_path: str | Path | None = None,
    imap_only: bool = False,
    smtp_only: bool = False,
) -> dict[str, Any]:
    mailbox = load_account(account, config_path)
    imap_result = {"tested": not smtp_only, "ok": False, "error": ""}
    smtp_result = {"tested": not imap_only, "ok": False, "error": ""}

    if not smtp_only:
        try:
            test_imap_login(mailbox)
            imap_result["ok"] = True
        except Exception as exc:
            imap_result["error"] = str(exc)

    if not imap_only:
        try:
            test_smtp_login(mailbox)
            smtp_result["ok"] = True
        except Exception as exc:
            smtp_result["error"] = str(exc)

    status = "ok"
    if (imap_result["tested"] and not imap_result["ok"]) or (smtp_result["tested"] and not smtp_result["ok"]):
        status = "needs_attention"
    return {
        "account": mailbox.name,
        "provider": mailbox.provider,
        "imap": imap_result,
        "smtp": smtp_result,
        "test_login_status": status,
    }


def compose_email_body(subject: str, content: str, tone: str, to_name: str, sender_name: str) -> str:
    greeting_name = to_name or "there"
    sign_name = sender_name or "[Your Name]"
    stripped = content.strip()
    if tone == "formal":
        return (
            f"Hello {greeting_name},\n\n"
            f"I am writing regarding {subject}.\n\n"
            f"{stripped}\n\n"
            "Please let me know if you would like me to provide any additional detail.\n\n"
            f"Best regards,\n{sign_name}"
        )
    if tone == "support":
        return (
            f"Hello {greeting_name},\n\n"
            f"This message is about {subject}.\n\n"
            f"{stripped}\n\n"
            "If you need anything else, please reply to this email and I will follow up.\n\n"
            f"Regards,\n{sign_name}"
        )
    return (
        f"Hi {greeting_name},\n\n"
        f"I wanted to follow up on {subject}.\n\n"
        f"{stripped}\n\n"
        "Let me know if you want me to adjust anything or send a revised version.\n\n"
        f"Thanks,\n{sign_name}"
    )


def draft_email(
    *,
    subject: str,
    body: str,
    tone: str = "colleague",
    to_name: str = "",
    sender_name: str = "",
    output: str | None = None,
) -> dict[str, Any]:
    if tone not in {"colleague", "formal", "support"}:
        raise EmailClientError("tone must be colleague, formal, or support", code="invalid_request")
    draft = compose_email_body(
        subject=subject,
        content=body,
        tone=tone,
        to_name=to_name,
        sender_name=sender_name,
    )
    output_path = ""
    if output:
        output_path = str(Path(output).expanduser())
        Path(output_path).write_text(draft, encoding="utf-8")
    return {
        "status": "ok",
        "subject": subject,
        "tone": tone,
        "draft": draft,
        "output_path": output_path,
    }

