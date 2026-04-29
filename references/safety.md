# Deletion Safety Rules

## Model

- **Two-tier, not three.** Soft delete is the default path; hard delete is only allowed on UIDs already inside the Trash folder. There is no "INBOX → permanent delete" shortcut.
- **Every delete/restore/purge operation preserves an audit trail** at `config/audit.log`（项目根目录下） (JSONL, UTC timestamps).

## Tools

| Tool | Operates on | What it does |
|------|------------|---------------|
| `list_folders` | any | Lists IMAP folders and auto-detects the Trash folder name. |
| `trash_messages` | any folder except Trash | Moves UIDs into the Trash. Fails if `folder` already equals Trash. |
| `restore_messages` | Trash | Moves UIDs from Trash to `target_folder` (default `INBOX`). |
| `purge_messages` | **Trash only** | Hard-deletes UIDs. Rejects any UID it cannot fetch from Trash. Irreversible. |

All four accept:

- `uids` (required, list of stringified integers, ≤ 50 per call)
- `confirmed` (default `false`)
- `trash_folder` (optional override)
- `config_path` (optional, defaults to `config/accounts.json`)

## `confirmed` Gate

- `confirmed=false` is the default. The tool fetches message headers, returns `{"status": "preview", "messages": [...]}`, and **does not touch the mailbox**. Use this to obtain a human-reviewable list.
- `confirmed=true` performs the operation. Agents MUST only set this after a fresh user confirmation in the same turn.
- A previous confirmation never carries forward; each new batch needs a new yes.

## Hard-delete Protection

- `purge_messages` first lists the UIDs it sees in the Trash folder. If any provided UID cannot be matched in Trash, the call fails with `invalid_request` listing the unreachable UIDs — the tool never attempts a partial purge.
- This makes "硬删 INBOX 里那封" structurally impossible: you must first `trash_messages` it, then `purge_messages` it from Trash.

## Agent Behaviour Contract

1. **Preview first.** For any delete/restore/purge, retrieve and show the affected messages (UID, date, from, subject) to the user before executing.
2. **Fresh confirmation.** Ask yes/no in the current turn; only then call with `confirmed=true`. Old yes ≠ new yes.
3. **Default soft.** Reply to "删 / 删除 / 清理" with `trash_messages` only. After success, say 「已移至回收站『<Trash>』，可通过 `restore_messages` 恢复」.
4. **Opt-in hard.** `purge_messages` may only be used after the user explicitly says words like "硬删 / 彻底删除 / 不可恢复 / purge / 永久删除". If they just say "删", do soft delete.
5. **Batch limit.** Single call ≤ 50 UIDs. For larger sets, split, confirming each batch.
6. **Never chain.** Do not combine `trash_messages` and `purge_messages` in one user turn. The user must review the Trash between the two.

## Audit Log

Every successful `trash` / `restore` / `purge` appends:

```json
{"ts":"2026-04-18T12:34:56+00:00","action":"trash","account":"default-send","folder":"INBOX","trash_folder":"Deleted Messages","uids":["123"],"messages":[{"uid":"123","subject":"...","from":"..."}]}
```

Log location: `<dirname of accounts.json>/audit.log` (POSIX `0600`). Use it to answer "what did I delete recently" questions; it is not visible to remote parties and it is in `.gitignore`.
