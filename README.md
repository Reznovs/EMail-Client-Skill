# Email Client Skill

A standalone publishable skill project for mailbox setup, migration, inbox lookup, attachment download, draft generation, and sending email across Gmail, QQ, or custom IMAP/SMTP accounts.

**支持平台**: Claude Code, OpenAI Codex, 以及其他兼容的 AI 助手

## Layout

```text
email-client-skill/
├── README.md
├── SKILL.md
├── agents/                 # AI 平台适配配置
│   ├── claude.yaml        # Claude Code 配置
│   ├── codex.yaml         # OpenAI Codex 配置
│   └── openai.yaml        # OpenAI 通用配置
├── references/            # 参考文档
├── scripts/               # 本地工具脚本
└── tests/                 # 测试
```

## AI 平台适配

本 skill 已适配以下 AI 平台：

| 平台 | 配置文件 | 状态 |
|------|---------|------|
| Claude Code | `agents/claude.yaml` | ✅ 已验证 |
| OpenAI Codex | `agents/codex.yaml` | ✅ 已验证 |
| OpenAI | `agents/openai.yaml` | ✅ 已配置 |

### 在 Claude Code 中使用

```bash
# 测试账号配置
python3 scripts/mail_tools.py doctor_account --input-json '{}'

# 测试登录
python3 scripts/mail_tools.py test_login --input-json '{"account":"default-send"}'

# 列出邮件
python3 scripts/mail_tools.py list_messages --input-json '{"account":"default-send","limit":10}'

# 生成草稿
python3 scripts/mail_tools.py draft_email --input-json '{"subject":"主题","body":"内容"}'

# 发送邮件
python3 scripts/mail_tools.py send_email --input-json '{"account":"default-send","to":"recipient@example.com","subject":"主题","body":"内容"}'
```

## Supported Provider Boundary

- `gmail`
- `qq`
- `custom`

This project does not claim built-in presets for `outlook`, `163`, or other providers.

## Local Tool Call

Machine-facing entrypoint:

```bash
python3 scripts/mail_tools.py setup_account --input-json '{"account":"work","provider":"gmail","email":"user@example.com"}'
```

Other tool names:

- `migrate_config`
- `setup_account`
- `doctor_account`
- `test_login`
- `list_messages`
- `search_messages`
- `get_message`
- `download_attachments`
- `send_email`
- `draft_email`

## CLI Usage

Human-facing entrypoint:

```bash
python3 scripts/mail_client.py setup_account --account work --provider gmail --email user@example.com
python3 scripts/mail_client.py list_messages --account work --limit 10
python3 scripts/mail_client.py draft_email --subject "Project update" --body "The work is on track."
```

Unix wrapper:

```bash
./scripts/mail_client.sh list_messages --account work --limit 10
```

## Config

Default config path:

```text
~/.config/codex-mail/accounts.json
```

Override with:

```bash
export CODEX_MAIL_ACCOUNTS=/custom/path/accounts.json
```

The active writable schema is `v2`.

## Testing

运行测试确认各功能正常：

```bash
# 检查账号配置
python3 scripts/mail_tools.py doctor_account --input-json '{}'

# 测试登录
python3 scripts/mail_tools.py test_login --input-json '{"account":"your-account"}'

# 列出邮件
python3 scripts/mail_tools.py list_messages --input-json '{"account":"your-account","limit":5}'

# Python 单元测试
python3 -m pytest tests/test_mail_tools.py -v
```
