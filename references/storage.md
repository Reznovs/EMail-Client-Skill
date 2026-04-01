# Attachment Storage

## Modes

- `temp`: create a short-lived temp directory
- `archive`: save under a dated attachment archive

## Archive Shape

```text
~/Documents/CodexMail/attachments/<account>/<YYYY-MM-DD>/<uid>/
```

## Send Safety

Downloaded attachments are registered in an approval manifest inside the target directory.

Only attachments saved through `download_attachments` are allowed to pass into `send_email`.

## Reporting

Always report:

- the mode
- the full target directory path
- every saved filename

If there are no attachments, say so explicitly.
