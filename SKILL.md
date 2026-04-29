---
name: mail-ops-skill
description: "Use this skill for mailbox operations via the bundled Mail Ops Skill scripts: account setup, login checks, listing and searching messages, reading by UID, downloading attachments, drafting HTML, sending HTML mail (with attachments, inline images and ICS invites), and deletion (soft delete to Trash, restore, and hard delete that is only allowed from the Trash folder). All outgoing mail is HTML. See references/writing-style.md, references/safety.md and references/tool-calls.md."
---

# Mail Ops Skill (邮件运维工程师 Skill)

## Overview

Use this skill to perform mailbox operations through the bundled local scripts, not an external MCP server. All outgoing mail is HTML. Deletion is two-tier: **soft delete (move to Trash) is the default; hard delete is only permitted on UIDs that already live in the Trash folder.**

Supported providers: `gmail`, `qq`, `custom`.

## Quick Start

1. Classify the request as one of:
   - account setup or repair
   - mailbox lookup or reading
   - attachment download
   - drafting or sending
   - deletion / restoration
2. Read only the reference file you need:
   - provider and setup rules: `references/providers.md`
   - attachment storage behavior: `references/storage.md`
   - HTML writing style and send rules: `references/writing-style.md`
   - **deletion safety rules: `references/safety.md`**
   - machine-facing local tool-call contract: `references/tool-calls.md`
3. Use `python3 scripts/mail_tools.py <tool_name> --input-json '<json>'` for deterministic execution.
4. Use `python3 scripts/mail_client.py <command>` for a human-facing CLI.

## Workflow Rules

### Account Setup Or Repair

- Start with `doctor_account` when config state is unknown.
- Run `migrate_config` before any mailbox operation if the config is still `v1`.
- Use `setup_account` to create or update mailbox settings.
- Run `test_login` after credential or server changes.

### Mailbox Lookup Or Reading

- Use `search_messages` when the user provides keywords, sender hints, or subject clues.
- Use `list_messages` when the user wants recent mail or the request is vague.
- Use `get_message` only after you have a specific `UID`.

### Attachment Download

- Use `download_attachments` only after confirming the right `UID`.
- Default to `mode="temp"`.
- Use `mode="archive"` only when the user explicitly wants long-lived storage.

### Local File Registration

- Use `register_attachments` when the user wants to send local files (not downloaded from mailbox) as attachments.
- This registers the files in the approval manifest (`.codex-mail-attachments.json`) so `send_email` can accept them.
- Pass one or more file paths via the `files` parameter (string or string array).
- The tool validates each file exists and is not a symlink before registering.
- After registration, the files can be passed to `send_email`'s `attachments` parameter.
- Example flow: `register_attachments` → `send_email` with the same paths.

### Drafting Or Sending

- Use `draft_email` to generate a local HTML draft.
- Use `send_email` only when the sending account, recipients, subject, HTML body, and attachment paths are clear.
- **Attachment requirement:** Files passed to `send_email`'s `attachments` parameter must be in an approved directory (with a `.codex-mail-attachments.json` manifest). Files from `download_attachments` are auto-registered. For local files, call `register_attachments` first.
- Use `send_scheduled_email` when the user asks for delayed delivery. It calls the Resend API with a `scheduled_at` timestamp; no local AI or OS scheduler is required.
  - Resend API Key 优先从 sender.resend_api_key 读取，回退 `RESEND_API_KEY` 环境变量。
  - 未传 `to` 时自动使用 main 收件人。
  - When the domain is not yet verified at Resend, `from` defaults to `onboarding@resend.dev` and recipients are restricted to the registered email address.
- **All outgoing mail is HTML.** Compose the body with inline CSS so QQ Mail renders fonts, sizes, weights, colors, and backgrounds faithfully. Plain-text fallback is derived automatically — do not author it.
- Use inline images (`cid:`) for custom emoji; Unicode emoji can go straight in the HTML.
- Use `ics_event` / `ics_file` to attach a calendar invite when the user asks for a schedule.
- Apply the send checks in `references/writing-style.md` before every `send_email` call.

### Deletion / Restoration — SAFETY CRITICAL

**Before any delete call, you MUST:**

1. Identify candidate UIDs with `list_messages` / `search_messages`, read details with `get_message` if needed, and **show the list (UID, date, from, subject) to the user**.
2. Ask the user yes/no. Wait for an affirmative reply in this turn. A previous "yes" does **not** carry over to new batches.
3. Call the delete tool with `confirmed=true` only after that fresh affirmative reply. With `confirmed=false` (the default) the tool returns a `preview` and makes no changes — use this to satisfy step 1 when convenient.

**Two-tier model (enforced by the tool layer):**

- `trash_messages` — soft delete; moves UIDs from any folder to the Trash. **This is the default delete path.** After success, tell the user the Trash folder name and that `restore_messages` can recover them.
- `restore_messages` — moves UIDs from Trash back to a target folder (default `INBOX`).
- `purge_messages` — hard delete, **only accepts UIDs that are already in the Trash folder**. The tool rejects UIDs it cannot reach in Trash, so there is no "INBOX → permanent delete" path. The user must explicitly use words like "硬删 / 彻底删除 / 不可恢复 / purge" to unlock this tool.

**Never** call `purge_messages` on your own initiative; never bundle it with an initial "delete" request. The canonical flow is: `trash_messages` → (user reviews Trash) → optional `purge_messages` only if the user says so.

All delete / restore / purge operations append a JSONL entry to `config/audit.log`（项目根目录下） automatically; you do not need to maintain that log, but you may reference it when the user asks "what did I delete recently".

## Output Rules

- For `list_messages` / `search_messages`, lead with `UID`, date, sender, subject.
- For `get_message`, summarize first; expand only on request.
- For `download_attachments`, report mode, target dir, saved filenames.
- For `register_attachments`, report each registered file path and directory; list any errors.
- For `trash_messages` success: say **"已移至回收站『<name>』，可通过 `restore_messages` 恢复"**.
- For `purge_messages` success: say **"已永久删除，无法恢复"**.
- For any `preview` return (confirmed=false), present the message list and ask for confirmation; do not re-run with confirmed=true in the same turn without a fresh user yes.
- Do not present unsupported providers or external-only workflows as available options.
