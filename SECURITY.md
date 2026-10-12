# Security

This beta is a single-user local application. Do not expose it through a public tunnel, reverse proxy
or public interface. Loopback binding, per-launch sessions, same-origin checks and path containment
protect the browser boundary; they are not hosted accounts or tenant isolation.

For a suspected vulnerability, use GitHub's private vulnerability reporting on this repository if
available. If private reporting is unavailable, open an issue requesting a private contact without
posting an exploit, credentials or private designs. Never include API keys in an issue, screenshot,
log or exported support bundle. Revoke a key at its provider if it was exposed.

Use only versioned builds from a reviewed repository revision and verify the supplied SHA256SUMS.
The beta setup scripts are not signed, and generated hardware/firmware is not physically validated.
