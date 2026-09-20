# Security policy

This project handles sign-ins, session tokens and remote-access metadata, so security reports are taken
seriously.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Use GitHub's private reporting instead:
the **Security** tab of this repository, then **Report a vulnerability**. Include what you found, how to
reproduce it, and the version (`rustdesk-api version`).

You can expect an acknowledgement, and a fix or a clear answer as soon as it can be investigated. This is a
small project, so there is no guaranteed response time.

## Supported versions

Only the latest release and the `main` branch receive fixes.

## Hardening your own deployment

The README's *Security recommendations* section lists what to set before exposing the server: a strong
`SECRET_KEY`, TLS with `SECURE_COOKIES=true`, `ALLOW_REGISTRATION=false`, a restricted `CORS_ORIGINS`, and
optionally `WEBUI_ALLOWED_NETWORKS`. Several RustDesk client endpoints are unauthenticated by protocol
design, so prefer not to expose the API port directly to the internet without a reverse proxy or VPN.
