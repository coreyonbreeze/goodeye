# Security

GoodEye is a local tool. The board listens on `127.0.0.1` only and never on your network.

## What protects the board

- **Loopback only.** The server binds `127.0.0.1`. Other machines cannot reach it.
- **Host check.** Requests whose `Host` header is not `localhost`, `127.0.0.1` or `[::1]` get 403. This blocks DNS-rebinding pages from reading your store.
- **Write check.** Verdicts must be `application/json` (cross-site pages cannot send that without a preflight the server never approves) and must carry no foreign `Origin`. A website you visit cannot approve or reject your work.
- **No framing.** `X-Frame-Options: DENY` and `frame-ancestors 'none'` stop click-jacking.
- **Content Security Policy.** The board loads nothing from other origins. Uploaded files are served with a `sandbox` policy, so an SVG cannot run script.
- **Store-only reads.** File paths are resolved and must stay inside the store. IDs are validated.
- **No accounts, no network calls, no telemetry.** GoodEye never sends your work anywhere.

## What it does not protect against

- Other programs running as your user can read `~/.goodeye` and call the local port, like any local file or service.
- Do not expose the port with a tunnel or a reverse proxy. There is no login.

## Reporting a problem

Open a private security advisory on this repository (Security > Report a vulnerability). Please do not open a public issue for a vulnerability.
