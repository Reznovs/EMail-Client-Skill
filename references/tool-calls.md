# Local Tool Calls

## Machine-Facing Entrypoint

```bash
python3 scripts/mail_tools.py <tool_name> --input-json '<json>'
```

## Configuration

All tools (except `setup_account` and `migrate_config`) require a valid config file with `setup: 1`.

Config path defaults to `<project_root>/config/accounts.json`, overridable via `MAIL_OPS_ACCOUNTS` env var.

## Tool Names

### Setup & Diagnostics

- `setup_account` — 配置 sender + recipients，写入 config
  - `provider` (required): `qq` / `gmail` / `custom`
  - `email` (required): 发件人邮箱
  - `auth_code` (optional): 邮箱授权码
  - `display_name` (optional): 显示名
  - `login_user` (optional): 登录用户名，默认同 email
  - `resend_api_key` (optional): Resend API Key
  - `recipients` (optional): `[{"email":"x@y.com","name":"Name","main":true}, ...]`
  - `config_path` (optional): 配置文件路径
- `doctor_account` — 检查配置完整性
- `test_login` — 测试 IMAP/SMTP 登录
  - `imap_only` / `smtp_only` (optional): 只测一个通道
- `migrate_config` — v1→v2 迁移（旧格式兼容）

### Mailbox Operations（均自动使用 sender 账户）

- `list_messages` — 列出最近邮件
  - `folder` (default: INBOX), `limit` (default: 20)
- `search_messages` — 搜索邮件
  - `query` (required), `folder`, `scan`, `limit`
- `get_message` — 读取单封邮件
  - `uid` (required), `folder`
- `download_attachments` — 下载附件
  - `uid` (required), `mode` (temp|archive), `folder`
- `list_folders` — 列出文件夹

### Deletion（两阶段：先 preview，再 confirmed=true 执行）

- `trash_messages` — 软删到回收站
  - `uids` (required), `confirmed` (default: false), `folder`, `trash_folder`
- `restore_messages` — 从回收站恢复
  - `uids` (required), `confirmed` (default: false), `target_folder`, `trash_folder`
- `purge_messages` — 硬删（仅限回收站内，不可恢复）
  - `uids` (required), `confirmed` (default: false), `trash_folder`

### Sending

- `send_email` — 发送 HTML 邮件
  - `to` (optional): 收件人，未传时自动使用 main 收件人
  - `subject` (required), `html_body` / `plain_body`
  - `attachments`, `inline_images`, `ics_event` / `ics_content` / `ics_file`
- `send_scheduled_email` — 通过 Resend 定时发送
  - `to` (optional): 收件人，未传时自动使用 main 收件人
  - `subject`, `html_body`
  - `from_addr` (optional): 默认 sender.email
  - `api_key` (optional): 默认 sender.resend_api_key
  - `delay_minutes` / `scheduled_at` (二选一)
- `draft_email` — 生成邮件草稿（不发送）

### Attachments

- `register_attachments` — 注册本地文件到审批清单
- `download_attachments` — 下载邮件附件

## Result Shape

Successful calls return JSON objects.

Failures return:

```json
{
  "status": "error",
  "error_code": "not_configured",
  "message": "配置未完成，请先调用 setup_account"
}
```

Common error codes: `not_configured`, `invalid_setup`, `invalid_request`, `config_not_found`, `api_error`.

## Human CLI

```bash
python3 scripts/mail_client.py <command> [flags...]
```
