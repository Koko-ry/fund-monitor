# Security Policy

## Sensitive data

Never commit the following values:

- `PUSHPLUS_TOKEN`
- `STATE_API_TOKEN`
- `FEEDBACK_SIGNING_SECRET`
- GitHub, Cloudflare or other access tokens
- `fund_state.json` and `daily_log.jsonl` containing real investment records

Use GitHub Actions Secrets and Cloudflare Worker Secrets instead.

## Reporting a vulnerability

Please do not disclose exploitable vulnerabilities in a public issue. Open a private GitHub security advisory for this repository and include:

- affected component and version or commit;
- reproduction steps;
- expected impact;
- suggested mitigation, if available.

## Security model

- The monitor never places trades.
- Holdings change only after an explicit confirmation.
- Feedback links are HMAC-signed and expire.
- The state API requires a Bearer token.
- Cloudflare stores strategy state, not GitHub credentials.
