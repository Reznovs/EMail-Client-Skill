#!/usr/bin/env python3

from __future__ import annotations

import base64
import html
import imaplib
import json
import mimetypes
import os
import re
import shutil
import smtplib
import socket
import ssl
import tempfile
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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


def _resolve_default_config() -> Path:
    """跨平台的默认凭据文件路径。

    优先级：
    1. 环境变量 `MAIL_OPS_ACCOUNTS`（新）或 `CODEX_MAIL_ACCOUNTS`（旧，向后兼容）
    2. 项目本地：<项目根>/config/accounts.json（三平台统一）
    """
    for env_name in ("MAIL_OPS_ACCOUNTS", "CODEX_MAIL_ACCOUNTS"):
        value = os.environ.get(env_name)
        if value:
            return Path(value).expanduser()

    project_root = Path(__file__).resolve().parent.parent
    return project_root / "config" / "accounts.json"


DEFAULT_CONFIG = _resolve_default_config()
KEYRING_SERVICE = "mail-ops"

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


def check_setup(config_path: str | Path | None = None) -> dict[str, Any]:
    """检查配置是否就绪。setup==0 或缺失则报错，否则返回完整配置 dict。"""
    path = resolve_config_path(config_path)
    data = _load_json(path)
    if not data.get("setup"):
        raise EmailClientError(
            "配置未完成，请先调用 setup_account",
            code="not_configured",
        )
    return data


def _sender_to_account(sender: dict[str, Any]) -> AccountConfig:
    """将 sender dict 转换为 AccountConfig，供下游 MailClient / send_email 使用。"""
    provider = str(sender.get("provider") or "custom").strip().lower()
    preset = PROVIDER_PRESETS.get(provider, {})
    email = str(sender.get("email") or "").strip()
    if not email:
        raise EmailClientError("sender.email is required", code="invalid_config")

    login_user = str(sender.get("login_user") or email).strip()
    display_name = str(sender.get("display_name") or "").strip()
    auth_code = str(sender.get("auth_code") or "").strip()

    imap_raw = sender.get("imap")
    smtp_raw = sender.get("smtp")
    imap = _server_from_raw(imap_raw, fallback=preset.get("imap")) or (
        preset.get("imap") if preset else None
    )
    smtp = _server_from_raw(smtp_raw, fallback=preset.get("smtp")) or (
        preset.get("smtp") if preset else None
    )
    if imap is None or smtp is None:
        raise EmailClientError("sender.imap and sender.smtp are required", code="invalid_config")

    auth_mode = str(preset.get("auth_mode") or "auth_code").strip()

    return AccountConfig(
        name="sender",
        provider=provider,
        identity=IdentityConfig(email=email, login_user=login_user, display_name=display_name),
        auth=AuthConfig(
            mode=auth_mode,
            storage="config_file",
            secret=auth_code or auth_secret_placeholder(auth_mode),
            keyring_key=None,
        ),
        imap=imap,
        smtp=smtp,
        proxy=None,
    )


def load_sender(config_path: str | Path | None = None) -> AccountConfig:
    """从配置文件加载 sender 并返回 AccountConfig。"""
    data = check_setup(config_path)
    sender = data.get("sender")
    if not isinstance(sender, dict):
        raise EmailClientError("sender 配置缺失", code="invalid_config")
    return _sender_to_account(sender)


def get_main_recipient(config_path: str | Path | None = None) -> dict[str, str]:
    """获取标记为 main 的默认收件人。"""
    data = check_setup(config_path)
    recipients = data.get("recipients")
    if not isinstance(recipients, list) or not recipients:
        raise EmailClientError("recipients 配置为空", code="invalid_config")
    for r in recipients:
        if isinstance(r, dict) and r.get("main"):
            email = str(r.get("email") or "").strip()
            if not email:
                raise EmailClientError("main 收件人缺少 email 字段", code="invalid_config")
            return {"email": email, "name": str(r.get("name") or "").strip()}
    raise EmailClientError("未找到 main 标记的收件人", code="invalid_config")


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
    if os.name != "nt":
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
    temp_path = path.with_name(f".{path.name}.tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        if os.name != "nt":
            os.chmod(temp_path, 0o600)
        handle.write(render_config(data))
    os.replace(temp_path, path)
    if os.name != "nt":
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
        value = keyring.get_password(KEYRING_SERVICE, keyring_key)
        if value:
            return value
        # 向后兼容旧 service 名
        return keyring.get_password("codex-mail", keyring_key)
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
    result: dict[str, Any] = {"config": str(path)}
    if not path.exists():
        result["doctor_status"] = "needs_attention"
        result["issues"] = ["account config does not exist"]
        result["next_step"] = "run setup_account to create config."
        return result

    raw = _load_json(path)

    # 新格式：有 setup 字段
    if "setup" in raw:
        issues: list[str] = []
        notes: list[str] = []
        sender = raw.get("sender")
        if not isinstance(sender, dict):
            issues.append("sender 配置缺失")
        else:
            if not sender.get("email"):
                issues.append("sender.email 缺失")
            auth_code = str(sender.get("auth_code") or "").strip()
            if not auth_code:
                issues.append("sender.auth_code 缺失或使用占位符")
            notes.append(f"provider: {sender.get('provider', 'unknown')}")
            notes.append(f"email: {sender.get('email', 'unknown')}")

        recipients = raw.get("recipients")
        if not isinstance(recipients, list) or not recipients:
            issues.append("recipients 为空")
        else:
            has_main = any(isinstance(r, dict) and r.get("main") for r in recipients)
            if not has_main:
                issues.append("无 main 标记的收件人")
            notes.append(f"recipients: {len(recipients)}")

        result["doctor_status"] = "needs_attention" if issues else "ok"
        result["issues"] = issues
        result["notes"] = notes
        return result

    # 旧 v2 格式兼容
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
    result["accounts"] = []
    for name, account_raw in accounts.items():
        account_issues: list[str] = []
        notes: list[str] = []
        try:
            account = _account_from_v2(name, account_raw)
            if account.auth.storage == "config_file" and is_placeholder_secret(account.auth.secret):
                account_issues.append("auth.secret is missing or still uses a placeholder")
            notes.append(f"auth_mode: {account.auth.mode}")
            notes.append(f"secret_storage: {account.auth.storage}")
            if account.proxy:
                notes.append(
                    f"proxy: {account.proxy.type}://{account.proxy.host}:{account.proxy.port} (remote_dns={account.proxy.remote_dns})"
                )
        except EmailClientError as exc:
            account_issues.append(exc.message)
        result["accounts"].append(
            {
                "name": name,
                "status": "needs_attention" if account_issues else "ok",
                "issues": account_issues,
                "notes": notes,
            }
        )
        if account_issues:
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
    provider: str,
    email: str,
    auth_code: str | None = None,
    display_name: str | None = None,
    login_user: str | None = None,
    resend_api_key: str | None = None,
    recipients: list[dict[str, Any]] | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """配置 sender 和 recipients，写入新格式 JSON，setup 设为 1。"""
    provider_name = provider.strip().lower()
    mailbox_email = email.strip()
    if not provider_name:
        raise EmailClientError("provider is required", code="invalid_setup")
    if not mailbox_email:
        raise EmailClientError("email is required", code="invalid_setup")

    preset = PROVIDER_PRESETS.get(provider_name, {})
    if not preset:
        raise EmailClientError(
            f"不支持的 provider: {provider_name}，可选: {', '.join(PROVIDER_PRESETS.keys())}",
            code="invalid_setup",
        )

    imap_cfg = preset["imap"]
    smtp_cfg = preset["smtp"]

    final_display_name = (display_name or mailbox_email).strip()
    final_login_user = (login_user or mailbox_email).strip()
    final_auth_code = (auth_code or "").strip()

    sender: dict[str, Any] = {
        "email": mailbox_email,
        "login_user": final_login_user,
        "display_name": final_display_name,
        "provider": provider_name,
        "auth_code": final_auth_code,
        "imap": {"host": imap_cfg.host, "port": imap_cfg.port, "security": imap_cfg.security},
        "smtp": {"host": smtp_cfg.host, "port": smtp_cfg.port, "security": smtp_cfg.security},
    }
    if resend_api_key and resend_api_key.strip():
        sender["resend_api_key"] = resend_api_key.strip()

    final_recipients: list[dict[str, Any]] = []
    if recipients:
        has_main = False
        for r in recipients:
            r_email = str(r.get("email") or "").strip()
            if not r_email:
                continue
            is_main = bool(r.get("main"))
            if is_main:
                has_main = True
            final_recipients.append({
                "email": r_email,
                "name": str(r.get("name") or "").strip(),
                "main": is_main,
            })
        if not has_main and final_recipients:
            final_recipients[0]["main"] = True

    document: dict[str, Any] = {
        "setup": 1,
        "sender": sender,
        "recipients": final_recipients,
    }
    config = resolve_config_path(config_path)
    _write_json(config, document)

    secret_status = "provided" if final_auth_code else "placeholder"
    return {
        "status": "ok",
        "provider": provider_name,
        "email": mailbox_email,
        "config": str(config),
        "secret_status": secret_status,
        "recipients_count": len(final_recipients),
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


def register_attachments(
    *,
    files: str | list[str],
) -> dict[str, Any]:
    """将本地文件注册到附件审批清单，使其可用于 send_email。

    每个文件所在目录会自动创建/更新 .codex-mail-attachments.json。
    """
    if isinstance(files, str):
        files = [files]
    if not files:
        raise EmailClientError("files list is empty", code="invalid_request")

    registered: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    # 按目录分组处理
    dir_files: dict[Path, list[Path]] = {}
    for raw_path in files:
        candidate = Path(raw_path).expanduser()
        if candidate.is_symlink():
            errors.append({"file": raw_path, "error": "symlink not allowed"})
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            errors.append({"file": raw_path, "error": "file not found"})
            continue
        if not resolved.is_file():
            errors.append({"file": raw_path, "error": "not a regular file"})
            continue
        parent = resolved.parent
        dir_files.setdefault(parent, []).append(resolved)

    # 逐目录更新 manifest
    for parent, paths in dir_files.items():
        manifest = _attachment_manifest_path(parent)
        existing: set[str] = set()
        if manifest.is_file():
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                existing = set(payload.get("approved_files", []))
            except (json.JSONDecodeError, TypeError):
                pass
        for p in paths:
            existing.add(p.name)
            registered.append({"file": str(p), "directory": str(parent)})

        payload = {
            "version": 1,
            "approved_files": sorted(existing),
        }
        manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest.chmod(0o600)

    return {
        "status": "ok",
        "registered": registered,
        "errors": errors if errors else None,
    }


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


def _imap_utf7_decode(name: str) -> str:
    """解码 IMAP modified UTF-7 文件夹名（RFC 3501 §5.1.3）。"""
    if "&" not in name:
        return name
    out: list[str] = []
    i = 0
    while i < len(name):
        ch = name[i]
        if ch == "&":
            end = name.find("-", i + 1)
            if end == -1:
                out.append(name[i:])
                break
            segment = name[i + 1:end]
            if segment == "":
                out.append("&")
            else:
                b64 = segment.replace(",", "/") + "=" * (-len(segment) % 4)
                try:
                    out.append(base64.b64decode(b64).decode("utf-16-be"))
                except Exception:
                    out.append("&" + segment + "-")
            i = end + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


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

    def select_folder(self, folder: str, readonly: bool = True) -> None:
        mailbox = folder
        if " " in mailbox and not (mailbox.startswith('"') and mailbox.endswith('"')):
            mailbox = f'"{mailbox}"'
        status, _ = self._require_imap().select(mailbox, readonly=readonly)
        if status != "OK":
            raise RuntimeError(f"failed to open mailbox folder: {folder}")

    def list_folders(self) -> list[dict[str, str]]:
        status, data = self._require_imap().list()
        if status != "OK" or not data:
            return []
        folders: list[dict[str, str]] = []
        pattern = re.compile(rb'^\((?P<attrs>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.+)$')
        for raw in data:
            if raw is None:
                continue
            line = raw if isinstance(raw, bytes) else bytes(raw)
            m = pattern.match(line.strip())
            if not m:
                continue
            name_raw = m.group("name").decode("utf-8", errors="replace").strip()
            if name_raw.startswith('"') and name_raw.endswith('"'):
                name_raw = name_raw[1:-1]
            # IMAP UTF-7 decoding for folder names
            try:
                decoded_name = _imap_utf7_decode(name_raw)
            except Exception:
                decoded_name = name_raw
            attrs = m.group("attrs").decode("utf-8", errors="replace")
            delim = m.group("delim").decode("utf-8", errors="replace").strip('"')
            folders.append({"name": decoded_name, "raw_name": name_raw, "attrs": attrs, "delimiter": delim})
        return folders

    def move_uids(self, uids: list[bytes], dest_folder: str) -> None:
        if not uids:
            return
        imap = self._require_imap()
        uid_set = b",".join(uids)
        mailbox = dest_folder
        if " " in mailbox and not (mailbox.startswith('"') and mailbox.endswith('"')):
            mailbox = f'"{mailbox}"'
        # 优先 UID MOVE（RFC 6851），失败回退 COPY + STORE \Deleted + EXPUNGE
        try:
            status, _ = imap.uid("MOVE", uid_set, mailbox)
            if status == "OK":
                return
        except Exception:
            pass
        status, _ = imap.uid("COPY", uid_set, mailbox)
        if status != "OK":
            raise RuntimeError(f"failed to copy to {dest_folder}")
        imap.uid("STORE", uid_set, "+FLAGS", "(\\Deleted)")
        try:
            imap.uid("EXPUNGE", uid_set)
        except Exception:
            imap.expunge()

    def store_flags(self, uids: list[bytes], flags: str, operation: str = "+") -> None:
        if not uids:
            return
        if operation not in {"+", "-"}:
            raise ValueError("operation must be '+' or '-'")
        imap = self._require_imap()
        uid_set = b",".join(uids)
        status, _ = imap.uid("STORE", uid_set, f"{operation}FLAGS", flags)
        if status != "OK":
            raise RuntimeError(f"failed to {operation}FLAGS {flags}")

    def expunge_uids(self, uids: list[bytes]) -> None:
        if not uids:
            return
        imap = self._require_imap()
        uid_set = b",".join(uids)
        try:
            status, _ = imap.uid("EXPUNGE", uid_set)
            if status == "OK":
                return
        except Exception:
            pass
        imap.expunge()

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
    html_body: str,
    plain_body: str | None = None,
    attachments: list[str] | None = None,
    inline_images: list[dict[str, str]] | None = None,
    ics_content: str | None = None,
    ics_filename: str = "invite.ics",
) -> dict[str, Any]:
    if not html_body or not html_body.strip():
        raise EmailClientError("html_body is required", code="invalid_request")
    recipients = normalize_recipients(to)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{account.display_name} <{account.email}>"
    msg["To"] = ", ".join(recipients)

    # 纯文本降级版本（用于反垃圾扫描 / 不支持 HTML 的客户端）
    fallback_plain = plain_body if plain_body is not None else derive_plain_from_html(html_body)
    msg.set_content(fallback_plain or "(HTML content)", charset="utf-8")
    msg.add_alternative(html_body, subtype="html", charset="utf-8")

    # 内联图片（表情/自定义图片），以 cid: 方式在 HTML 中引用
    if inline_images:
        html_part = msg.get_payload()[-1]
        for item in inline_images:
            cid = (item.get("cid") or "").strip()
            path_value = (item.get("path") or "").strip()
            if not cid or not path_value:
                raise EmailClientError("inline image requires cid and path", code="invalid_request")
            img_path = _validate_send_attachment(path_value)
            mime_type, _ = mimetypes.guess_type(str(img_path))
            if not mime_type or not mime_type.startswith("image/"):
                raise EmailClientError(
                    f"inline image must be an image file: {img_path}", code="invalid_request"
                )
            maintype, subtype = mime_type.split("/", 1)
            html_part.add_related(
                img_path.read_bytes(),
                maintype=maintype,
                subtype=subtype,
                cid=f"<{cid}>",
                filename=img_path.name,
            )

    attached_files: list[str] = []
    for attachment in attachments or []:
        path = _validate_send_attachment(attachment)
        mime_type, _ = mimetypes.guess_type(str(path))
        if mime_type and "/" in mime_type:
            maintype, subtype = mime_type.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )
        attached_files.append(str(path))

    # 日程 ICS（作为 text/calendar 附件，QQ 邮箱会识别为日历邀请）
    if ics_content:
        ics_bytes = ics_content.encode("utf-8")
        msg.add_attachment(
            ics_bytes,
            maintype="text",
            subtype="calendar",
            filename=ics_filename,
            params={"method": "REQUEST", "charset": "utf-8", "name": ics_filename},
        )

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
        "inline_images": [item.get("cid", "") for item in (inline_images or [])],
        "ics_attached": bool(ics_content),
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
    config_path: str | Path | None = None,
    folder: str = DEFAULT_FOLDER,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    mailbox = load_sender(config_path)
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
    query: str = "",
    config_path: str | Path | None = None,
    folder: str = DEFAULT_FOLDER,
    scan: int = DEFAULT_SCAN,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    mailbox = load_sender(config_path)
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
    uid: str,
    config_path: str | Path | None = None,
    folder: str = DEFAULT_FOLDER,
) -> dict[str, Any]:
    mailbox = load_sender(config_path)
    with MailClient(mailbox) as client:
        client.select_folder(folder)
        msg = client.fetch_message(uid.encode())
    return {"status": "ok", "account": mailbox.name, "folder": folder, "message": build_message_detail(uid, msg)}


def download_attachments(
    *,
    uid: str,
    mode: str = "temp",
    config_path: str | Path | None = None,
    folder: str = DEFAULT_FOLDER,
) -> dict[str, Any]:
    if mode not in {"temp", "archive"}:
        raise EmailClientError("mode must be temp or archive", code="invalid_request")
    mailbox = load_sender(config_path)
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


# ---------------------------------------------------------------------------
# 文件夹 / 软删 / 恢复 / 硬删
# ---------------------------------------------------------------------------

# 常见回收站文件夹名（不区分大小写 / 含前后空白）
TRASH_CANDIDATES = (
    "trash",
    "deleted",
    "deleted messages",
    "deleted items",
    "[gmail]/trash",
    "已删除",
    "已删除邮件",
    "垃圾箱",
    "废件箱",
    "废纸篓",
)


def _audit_log_path() -> Path:
    return DEFAULT_CONFIG.parent / "audit.log"


def _append_audit(entry: dict[str, Any]) -> None:
    path = _audit_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
    entry = {"ts": datetime.now(tz=timezone.utc).isoformat(), **entry}
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _detect_trash_folder(folders: list[dict[str, str]], override: str | None = None) -> str | None:
    if override:
        for item in folders:
            if item["name"] == override or item.get("raw_name") == override:
                return item.get("raw_name") or item["name"]
        return override
    # 1) 按 IMAP 属性 \Trash 匹配
    for item in folders:
        if "\\Trash" in (item.get("attrs") or ""):
            return item.get("raw_name") or item["name"]
    # 2) 按常见名称匹配
    names = {item["name"].lower(): item for item in folders}
    for candidate in TRASH_CANDIDATES:
        if candidate in names:
            item = names[candidate]
            return item.get("raw_name") or item["name"]
    # 3) 模糊匹配
    for item in folders:
        lower = item["name"].lower()
        if "trash" in lower or "删除" in item["name"] or "垃圾" in item["name"] or "废" in item["name"]:
            return item.get("raw_name") or item["name"]
    return None


def _normalize_uids(uids: Any) -> list[bytes]:
    if isinstance(uids, (str, int, bytes)):
        uids = [uids]
    if not isinstance(uids, list):
        raise EmailClientError("uids must be a list", code="invalid_request")
    normalized: list[bytes] = []
    for item in uids:
        value = str(item).strip()
        if not value or not value.isdigit():
            raise EmailClientError(f"invalid uid: {item!r}", code="invalid_request")
        normalized.append(value.encode())
    if not normalized:
        raise EmailClientError("at least one uid is required", code="invalid_request")
    if len(normalized) > 50:
        raise EmailClientError("batch too large: max 50 uids per call", code="invalid_request")
    return normalized


def _collect_previews(client: "MailClient", uids: list[bytes]) -> list[dict[str, str]]:
    previews: list[dict[str, str]] = []
    for uid in uids:
        try:
            header = client.fetch_headers(uid)
        except Exception as exc:
            previews.append({"uid": uid.decode(), "error": str(exc)})
            continue
        previews.append(header)
    return previews


def list_folders(
    *,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    mailbox = load_sender(config_path)
    with MailClient(mailbox) as client:
        folders = client.list_folders()
    trash = _detect_trash_folder(folders)
    return {
        "status": "ok",
        "account": mailbox.name,
        "folders": folders,
        "trash_folder": trash,
    }


def trash_messages(
    *,
    uids: list[str] | str,
    confirmed: bool = False,
    folder: str = DEFAULT_FOLDER,
    trash_folder: str | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """软删：从 folder 移到回收站。confirmed=False 时只返回预览，不执行。"""
    mailbox = load_sender(config_path)
    uid_list = _normalize_uids(uids)
    with MailClient(mailbox) as client:
        folders = client.list_folders()
        resolved_trash = _detect_trash_folder(folders, override=trash_folder)
        if not resolved_trash:
            raise EmailClientError(
                "could not locate a Trash folder; pass trash_folder explicitly",
                code="invalid_request",
            )
        # 禁止对"本身就在回收站"的 UID 再软删（避免循环）
        if _folder_equals(folder, resolved_trash):
            raise EmailClientError(
                "source folder is already the trash folder; use purge_messages to hard-delete",
                code="invalid_request",
            )

        client.select_folder(folder, readonly=True)
        previews = _collect_previews(client, uid_list)
        if not confirmed:
            return {
                "status": "preview",
                "action": "trash",
                "account": mailbox.name,
                "folder": folder,
                "trash_folder": resolved_trash,
                "messages": previews,
                "note": "confirmed=false → 未执行。让用户确认后再以 confirmed=true 重新调用。",
            }

        client.select_folder(folder, readonly=False)
        client.move_uids(uid_list, resolved_trash)

    _append_audit(
        {
            "action": "trash",
            "account": mailbox.name,
            "folder": folder,
            "trash_folder": resolved_trash,
            "uids": [u.decode() for u in uid_list],
            "messages": previews,
        }
    )
    return {
        "status": "ok",
        "action": "trash",
        "account": mailbox.name,
        "folder": folder,
        "trash_folder": resolved_trash,
        "moved_uids": [u.decode() for u in uid_list],
        "messages": previews,
        "note": f"已移至回收站『{resolved_trash}』，可通过 restore_messages 恢复。",
    }


def restore_messages(
    *,
    uids: list[str] | str,
    confirmed: bool = False,
    target_folder: str = DEFAULT_FOLDER,
    trash_folder: str | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """从回收站恢复到 target_folder。"""
    mailbox = load_sender(config_path)
    uid_list = _normalize_uids(uids)
    with MailClient(mailbox) as client:
        folders = client.list_folders()
        resolved_trash = _detect_trash_folder(folders, override=trash_folder)
        if not resolved_trash:
            raise EmailClientError("could not locate Trash folder", code="invalid_request")
        client.select_folder(resolved_trash, readonly=True)
        previews = _collect_previews(client, uid_list)
        if not confirmed:
            return {
                "status": "preview",
                "action": "restore",
                "account": mailbox.name,
                "trash_folder": resolved_trash,
                "target_folder": target_folder,
                "messages": previews,
                "note": "confirmed=false → 未执行。让用户确认后再以 confirmed=true 重新调用。",
            }
        client.select_folder(resolved_trash, readonly=False)
        client.move_uids(uid_list, target_folder)

    _append_audit(
        {
            "action": "restore",
            "account": mailbox.name,
            "trash_folder": resolved_trash,
            "target_folder": target_folder,
            "uids": [u.decode() for u in uid_list],
            "messages": previews,
        }
    )
    return {
        "status": "ok",
        "action": "restore",
        "account": mailbox.name,
        "trash_folder": resolved_trash,
        "target_folder": target_folder,
        "restored_uids": [u.decode() for u in uid_list],
        "messages": previews,
        "note": f"已从『{resolved_trash}』恢复到『{target_folder}』。",
    }


def purge_messages(
    *,
    uids: list[str] | str,
    confirmed: bool = False,
    trash_folder: str | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """硬删：只能对"已在回收站"的 UID 执行，且需要 confirmed=true。不可恢复。"""
    mailbox = load_sender(config_path)
    uid_list = _normalize_uids(uids)
    with MailClient(mailbox) as client:
        folders = client.list_folders()
        resolved_trash = _detect_trash_folder(folders, override=trash_folder)
        if not resolved_trash:
            raise EmailClientError("could not locate Trash folder", code="invalid_request")
        client.select_folder(resolved_trash, readonly=True)
        previews = _collect_previews(client, uid_list)
        # 校验每个 UID 都能在 Trash 里 fetch 到 header（否则拒绝）
        missing = [p for p in previews if p.get("error")]
        if missing:
            raise EmailClientError(
                "purge_messages only accepts UIDs already in the Trash folder; "
                f"the following are not reachable there: {[p['uid'] for p in missing]}",
                code="invalid_request",
                details={"unreachable": missing, "trash_folder": resolved_trash},
            )
        if not confirmed:
            return {
                "status": "preview",
                "action": "purge",
                "account": mailbox.name,
                "trash_folder": resolved_trash,
                "messages": previews,
                "note": "confirmed=false → 未执行。硬删不可恢复，请让用户二次明确后再以 confirmed=true 调用。",
            }
        client.select_folder(resolved_trash, readonly=False)
        client.store_flags(uid_list, "(\\Deleted)", operation="+")
        client.expunge_uids(uid_list)

    _append_audit(
        {
            "action": "purge",
            "account": mailbox.name,
            "trash_folder": resolved_trash,
            "uids": [u.decode() for u in uid_list],
            "messages": previews,
        }
    )
    return {
        "status": "ok",
        "action": "purge",
        "account": mailbox.name,
        "trash_folder": resolved_trash,
        "purged_uids": [u.decode() for u in uid_list],
        "messages": previews,
        "note": "已永久删除，无法恢复。",
    }


def _folder_equals(a: str, b: str) -> bool:
    def norm(x: str) -> str:
        return x.strip().strip('"').lower()
    return norm(a) == norm(b)


def send_email_tool(
    *,
    to: str | list[str] | None = None,
    subject: str = "",
    html_body: str | None = None,
    plain_body: str | None = None,
    config_path: str | Path | None = None,
    attachments: list[str] | None = None,
    inline_images: list[dict[str, str]] | None = None,
    ics_content: str | None = None,
    ics_file: str | None = None,
    ics_event: dict[str, Any] | None = None,
    ics_filename: str = "invite.ics",
) -> dict[str, Any]:
    mailbox = load_sender(config_path)

    # to 未传时自动使用 main 收件人
    if to is None:
        main = get_main_recipient(config_path)
        to = main["email"]

    final_html_body = html_body
    if final_html_body is None:
        final_html_body = compose_email_body(subject=subject, content="")

    # ICS 来源：ics_content > ics_file > ics_event
    final_ics = ics_content
    if final_ics is None and ics_file:
        final_ics = Path(ics_file).expanduser().read_text(encoding="utf-8")
    if final_ics is None and ics_event:
        final_ics = build_ics(ics_event, organizer_email=mailbox.email)

    return send_email(
        mailbox,
        to=to,
        subject=subject,
        html_body=final_html_body,
        plain_body=plain_body,
        attachments=attachments,
        inline_images=inline_images,
        ics_content=final_ics,
        ics_filename=ics_filename,
    )


def test_login(
    *,
    config_path: str | Path | None = None,
    imap_only: bool = False,
    smtp_only: bool = False,
) -> dict[str, Any]:
    mailbox = load_sender(config_path)
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


def derive_plain_from_html(html_text: str) -> str:
    """从 HTML 自动提取纯文本降级版本，用于 multipart/alternative 的 text/plain 部分。"""
    if not html_text:
        return ""
    return clean_html_text(html_text)


def _ics_escape(text: str) -> str:
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _ics_format_dt(value: Any) -> str:
    """接受 datetime 或形如 '2026-04-20 14:00' / '2026-04-20T14:00:00' 的字符串，
    统一输出为 ICS 要求的 UTC 时间 `YYYYMMDDTHHMMSSZ`。"""
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip().replace("/", "-")
        # 支持 '2026-04-20 14:00' 或 '2026-04-20T14:00:00'
        raw = raw.replace(" ", "T")
        # 尝试多种格式
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        else:
            raise EmailClientError(f"invalid datetime for ICS: {value}", code="invalid_request")
    if dt.tzinfo is None:
        # 视为本地时间，转为 UTC
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(event: dict[str, Any], *, organizer_email: str = "") -> str:
    """根据事件字典构建 ICS 文本。字段：summary, start, end, location, description, attendees(list)。"""
    summary = event.get("summary") or event.get("title") or "Meeting"
    start = event.get("start")
    end = event.get("end") or start
    if not start:
        raise EmailClientError("ics event requires start", code="invalid_request")
    dtstart = _ics_format_dt(start)
    dtend = _ics_format_dt(end)
    dtstamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    uid = event.get("uid") or f"{uuid.uuid4()}@mail-ops-skill"
    location = event.get("location", "")
    description = event.get("description", "")
    attendees = event.get("attendees") or []

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Mail Ops Skill//ZH//",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{dtstart}",
        f"DTEND:{dtend}",
        f"SUMMARY:{_ics_escape(summary)}",
    ]
    if location:
        lines.append(f"LOCATION:{_ics_escape(location)}")
    if description:
        lines.append(f"DESCRIPTION:{_ics_escape(description)}")
    if organizer_email:
        lines.append(f"ORGANIZER;CN={organizer_email}:mailto:{organizer_email}")
    for att in attendees:
        if isinstance(att, dict):
            email_addr = att.get("email", "")
            cn = att.get("name") or email_addr
        else:
            email_addr = str(att)
            cn = email_addr
        if email_addr:
            lines.append(
                f"ATTENDEE;CN={_ics_escape(cn)};RSVP=TRUE:mailto:{email_addr}"
            )
    lines.extend(["END:VEVENT", "END:VCALENDAR"])
    return "\r\n".join(lines) + "\r\n"


def compose_email_body(subject: str = "", content: str = "", **_: Any) -> str:
    """生成一个带 QQ 邮箱友好内联样式的 HTML 壳。调用方通常直接提供完整 HTML；
    此函数仅在未提供 HTML 时作为兜底包裹 content 文本。"""
    safe_content = html.escape(content or "").replace("\n\n", "</p><p>").replace("\n", "<br>")
    body_inner = f"<p>{safe_content}</p>" if safe_content else ""
    subj = html.escape(subject or "").strip()
    header = f'<h3 style="margin:0 0 10px;color:#1a73e8;">{subj}</h3>' if subj else ""
    return (
        '<div style="font-family:\'Microsoft YaHei\',\'PingFang SC\',Arial,sans-serif;'
        'font-size:14px;color:#222;line-height:1.7;">'
        f"{header}{body_inner}"
        "</div>"
    )


def draft_email(
    *,
    subject: str,
    body: str,
    to_name: str = "",
    sender_name: str = "",
    output: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    html_draft = compose_email_body(subject=subject, content=body)
    output_path = ""
    if output:
        output_path = str(Path(output).expanduser())
        Path(output_path).write_text(html_draft, encoding="utf-8")
    return {
        "status": "ok",
        "subject": subject,
        "html_draft": html_draft,
        "text_preview": derive_plain_from_html(html_draft),
        "output_path": output_path,
    }


# ---------------------------------------------------------------------------
# 定时发送邮件（Resend API 策略一）
# ---------------------------------------------------------------------------

RESEND_API_BASE = "https://api.resend.com/emails"


def send_scheduled_email(
    *,
    to: str | list[str] | None = None,
    subject: str = "",
    html_body: str = "",
    from_addr: str | None = None,
    api_key: str | None = None,
    delay_minutes: int | None = None,
    scheduled_at: str | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """通过 Resend API 创建定时邮件任务。纯 HTTP，零系统依赖，跨平台。

    参数:
        to: 收件人地址（字符串或列表）。未传时自动使用 main 收件人。
        subject: 邮件主题。
        html_body: HTML 正文。
        from_addr: 发件人地址。默认从 sender.email 读取，回退环境变量 RESEND_FROM。
        api_key: Resend API Key。优先从 sender.resend_api_key 读取，回退环境变量。
        delay_minutes: 几分钟后发送（与 scheduled_at 二选一）。
        scheduled_at: 明确的 ISO 8601 字符串（与 delay_minutes 二选一）。
        config_path: 配置文件路径。
    """
    # to 未传时自动使用 main 收件人
    if to is None:
        main = get_main_recipient(config_path)
        to = main["email"]

    # 优先从配置读取 sender 的 key 和 email
    sender_cfg: dict[str, Any] | None = None
    try:
        data = check_setup(config_path)
        sender_cfg = data.get("sender") if isinstance(data.get("sender"), dict) else None
    except Exception:
        pass

    key = (
        api_key
        or (sender_cfg or {}).get("resend_api_key")
        or os.environ.get("RESEND_API_KEY", "")
    ).strip()
    if not key:
        raise EmailClientError(
            "Resend API Key 缺失。请在 sender.resend_api_key 中配置或设置 RESEND_API_KEY 环境变量。",
            code="missing_api_key",
        )

    sender = (
        from_addr
        or (sender_cfg or {}).get("email")
        or os.environ.get("RESEND_FROM", "onboarding@resend.dev")
    ).strip()
    if not sender:
        sender = "onboarding@resend.dev"
    if not key:
        raise EmailClientError(
            "Resend API Key 缺失。请设置 RESEND_API_KEY 环境变量或在参数中传入 api_key。",
            code="missing_api_key",
        )

    sender = (from_addr or os.environ.get("RESEND_FROM", "onboarding@resend.dev")).strip()
    if not sender:
        sender = "onboarding@resend.dev"

    recipients = normalize_recipients(to)

    if scheduled_at and delay_minutes is not None:
        raise EmailClientError(
            "scheduled_at 与 delay_minutes 不能同时传入", code="invalid_request"
        )

    if scheduled_at:
        final_scheduled = scheduled_at
    elif delay_minutes is not None:
        if delay_minutes < 1:
            raise EmailClientError("delay_minutes 至少为 1", code="invalid_request")
        dt = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
        final_scheduled = dt.isoformat()
    else:
        raise EmailClientError("必须传入 scheduled_at 或 delay_minutes", code="invalid_request")

    payload = {
        "from": sender,
        "to": recipients,
        "subject": subject,
        "html": html_body,
        "scheduled_at": final_scheduled,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        RESEND_API_BASE,
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "MailOpsSkill/1.0 (Python-urllib)",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            result = json.loads(body)
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8") if exc.fp else ""
        raise EmailClientError(
            f"Resend API error {exc.code}: {err_body}",
            code="api_error",
            details={"status": exc.code, "response": err_body},
        ) from exc
    except Exception as exc:
        raise EmailClientError(f"调用 Resend API 失败: {exc}", code="api_error") from exc

    return {
        "status": "ok",
        "provider": "resend",
        "to": recipients,
        "subject": subject,
        "scheduled_at": final_scheduled,
        "resend_id": result.get("id"),
        "raw_response": result,
    }

