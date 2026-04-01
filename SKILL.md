---
name: email-client-skill
description: "Use this skill when Codex needs to handle mailbox work with the bundled email-client-skill project: inspect or migrate a mail config, set up an account, test login, list or search messages, read a message by UID, download attachments, draft an email, or send an email. Prefer the bundled local tool-call layer for deterministic execution, and use the references for provider rules, storage behavior, and writing style."
---

# Email Client Skill

## Overview

Use this skill to perform email work through the bundled local scripts, not through an external MCP dependency. Keep the workflow tight, stay inside the project boundary, and prefer deterministic local tool calls when execution is required.

The supported provider boundary is: `gmail`, `qq`, and `custom`.

## Quick Start

1. Classify the request as one of:
   - account setup or repair
   - mailbox lookup or reading
   - attachment download
   - drafting or sending
2. Read only the reference file you need:
   - provider and setup rules: `references/providers.md`
   - attachment storage behavior: `references/storage.md`
   - drafting tone and send rules: `references/writing-style.md`
   - machine-facing local tool-call contract: `references/tool-calls.md`
3. Use `python3 scripts/mail_tools.py <tool_name> --input-json '<json>'` when deterministic execution is needed.
4. Use `python3 scripts/mail_client.py <command>` only when a human-facing CLI is more appropriate than JSON output.

## Workflow Rules

### Account Setup Or Repair

- Start with `doctor_account` when config state is unknown.
- Run `migrate_config` before any mailbox operation if the config is still `v1`.
- Use `setup_account` to create or update mailbox settings.
- Run `test_login` after credential or server changes before moving on to inbox or send actions.

### Mailbox Lookup Or Reading

- Use `search_messages` when the user provides keywords, sender hints, or subject clues.
- Use `list_messages` when the user wants recent mail or the request is vague.
- Use `get_message` only after you have a specific `UID`.
- If the user describes a message vaguely, identify the candidate message first, then read it.

### Attachment Download

- Use `download_attachments` only after confirming the right `UID`.
- Default to `mode="temp"`.
- Use `mode="archive"` only when the user explicitly wants long-lived storage.

### Drafting Or Sending

- Use `draft_email` to generate a local draft when the user wants a complete email body or an output file.
- Draft in chat first when recipients, subject, tone, body, or attachments are incomplete or ambiguous.
- Use `send_email` only when the sending account, recipients, subject, body, and attachment paths are clear.
- Use plain text by default. Only populate HTML content when the caller explicitly asks for it.
- Apply the send checks in `references/writing-style.md` before every `send_email` call.

## Output Rules

- For `list_messages` or `search_messages`, lead with `UID`, date, sender, and subject.
- For `get_message`, summarize first and expand only if the user asks for more detail.
- For `download_attachments`, always report the mode, full target directory path, and saved filenames.
- For drafted emails, write normal email prose with greeting, purpose, body, next step, and closing.
- Do not present unsupported providers or external-only workflows as available options.
