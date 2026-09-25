# Security

GoodEye is a local tool. By default the board listens on `127.0.0.1` only. Phone mode (`goodeye phone`) is opt-in and opens it to your local network behind a secret token.

## What protects the board

- **Loopback by default.** The server binds `127.0.0.1`. Other machines cannot reach it.
- **Phone mode is token-gated.** With `goodeye phone` the server binds all interfaces, and every request that is not from this machine needs a 32-character random token (`~/.goodeye/phone-token`, mode 600). The QR link sets it as an `HttpOnly`, `SameSite=Strict` cookie and redirects so the token leaves the address bar. Without it every request gets 403. Requests that name `localhost` must also come from loopback, so a device on the network cannot pose as local. `--new-token` signs every phone out; `--off` closes it again.
- **Host check.** Requests whose `Host` header is not `localhost`, `127.0.0.1` or `[::1]` get 403. This blocks DNS-rebinding pages from reading your store.
- **Write check.** Verdicts must be `application/json` (cross-site pages cannot send that without a preflight the server never approves) and must carry no foreign `Origin`. A website you visit cannot approve or reject your work.
- **No framing.** `X-Frame-Options: DENY` and `frame-ancestors 'none'` stop click-jacking.
- **Content Security Policy.** The board loads nothing from other origins. Uploaded files are served with a `sandbox` policy, so an SVG cannot run script.
- **Store-only reads.** File paths are resolved and must stay inside the store. IDs are validated.
- **No accounts, no telemetry.** GoodEye never sends your work anywhere. The one outside call is opt-in: `goodeye notify` posts the item title and a board link (no image, no reasoning) to the ntfy topic you choose. Anyone who knows a public ntfy topic name can read it, so use a long random name or your own ntfy server.

## What it does not protect against

- Other programs running as your user can read `~/.goodeye` and call the local port, like any local file or service.
- Phone mode uses plain HTTP on your network. Anyone who can watch your Wi-Fi traffic could read the token and your assets. Use it on networks you trust, or over Tailscale, which encrypts the connection.
- Do not expose the port to the internet with a tunnel or a reverse proxy.

## Reporting a problem

Open a private security advisory on this repository (Security > Report a vulnerability). Please do not open a public issue for a vulnerability.
