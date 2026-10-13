# Security policy

## Report a vulnerability privately

Use this repository's **Security tab → Report a vulnerability** to contact the
maintainers through GitHub's private vulnerability reporting:

https://github.com/Paskooter/jibo-platform/security/advisories/new

Include the plugin and Hermes versions, affected feature, impact and sanitized
steps to reproduce. Do **not** post codes, tokens, household addresses, private
hostnames, recordings or photos in public issues. Public issues are appropriate
for non-sensitive bugs only.

## Home networks only — never expose it to the Internet

The native bridge uses unencrypted `ws://` on port `8890`, path `/hermes`.
Pairing/token authentication is not encryption. A network observer can see
transport codes, tokens and conversation traffic; a captured token can
authorize robot control. Use it only on a trusted home network. Never forward
router ports or expose Jibo's bridge or the Hermes dashboard publicly.

A robot's firewall-off setting opens all ports, not just `8890`. The plugin
does not change that setting, add encryption or manage network isolation.

## Trust and authorization

This is an in-process Hermes platform plugin with the host user's privileges,
not a sandbox. Review source, dependencies and the installation scan before
enabling it. Store provider credentials in Hermes's private credential storage
and transport tokens in the selected profile's private configuration. Keep
both out of logs, screenshots, issue reports and shell arguments.

Jibo's five-character transport code and Hermes's eight-character authorization
code are separate. `jibo-local-user` authorizes shared local input, not a
recognized individual voice. Review effective tools, including global
plugin/MCP and provider-added tools; an empty platform tool list is not proof
that all tools are disabled. Do not enable all-platform authorization to bypass
approval. Camera access is off in the Agent Mode app.

New pairings save a robot identity pin separately from your device alias.
Identity mismatches prevent a native session. Existing tokens without this pin
remain unpinned until you pair again.

## Pairing recovery and revocation

The dashboard retains a received pairing token privately in process memory
until its configuration save is verified. That supports a save retry in the
same running process, not recovery after a crash or a genuinely lost response.
Reset pairing and pair again if the one-shot response is lost.

Host device/plugin removal does not unpair Jibo or revoke its saved token.
Uninstall Agent Mode on Jibo with **Remove the app and its data**, not
**Remove the app, keep its settings**, to reset its pairing; reinstall and
pair again. Confirm a new pairing code appears before transferring a robot.
Do not copy or manually alter robot token files.
See [README.md](README.md#reset-pairing).

## Source and dependency boundaries

This repository distributes host plugin code and documentation, not robot
firmware, native app/SDK implementations, raw catalogs or media assets.
Dependencies are not vendored and retain their own licenses. Source-artifact
and privacy checks support review; they are not a security audit.
