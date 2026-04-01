# Local Tool Calls

## Machine-Facing Entrypoint

```bash
python3 scripts/mail_tools.py <tool_name> --input-json '<json>'
```

## Tool Names

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

## Result Shape

Successful calls return JSON objects.

Failures return:

```json
{
  "status": "error",
  "error_code": "invalid_request",
  "message": "human-readable explanation"
}
```

## Human CLI

The human-facing wrapper delegates into the same core functions:

```bash
python3 scripts/mail_client.py <command> [flags...]
```
