# Security

## Reporting

Open a private issue or contact the maintainers with:

- affected version or commit
- reproduction steps
- expected impact
- any logs with secrets removed

## Secrets

Never commit `.env`, API keys, tokens, cookies, or supplier credentials. Use
environment variables from `.env.example`.

## Product boundaries

Spider-Qwen drafts RFQs only. It must not submit forms, send email, drive a
browser session, or run a code interpreter.
