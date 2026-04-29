# Provider Rules

## Supported Providers

- `gmail`
- `qq`
- `custom`

Do not present other built-in presets as available.

## Config Location

Default config file（三平台统一）:

```text
<project_root>/config/accounts.json
```

Override path with:

```bash
export MAIL_OPS_ACCOUNTS=/custom/path/accounts.json
```

## Setup Sequence

1. `doctor_account`
2. `migrate_config` when the file is still `v1`
3. `setup_account`
4. `test_login`

## Provider Notes

### Gmail

- Prefer `app_password`
- Add a proxy only when the current network requires it

### QQ

- Enable IMAP/SMTP in QQ Mail settings first
- Use an `auth_code`
- Do not add a proxy by default

### Custom

- Supply explicit IMAP and SMTP hosts
- Supply explicit ports when defaults are unknown
- Verify transport security mode carefully

## Minimal Setup Inputs

- `account`
- `provider`
- `email`
- `display_name`
- `login_user` when it differs from `email`
- `auth_secret` when available
- proxy settings only when required
