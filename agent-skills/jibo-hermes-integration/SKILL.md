---
name: jibo-hermes-integration
description: "Use when operating Jibo through the Hermes platform."
version: 1.4.4
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [jibo, hermes, native-bridge, asr, tts, esml, robotics]
---

# Jibo and Hermes integration

Use `jibo-platform` to connect Jibo's Agent Mode app to the owner's configured
Hermes provider. Hermes runs on the computer; the robot app owns speech, chat,
Stop and native actions. No companion process or firmware changes are needed.

## When to use

- Set up or troubleshoot a Jibo connection to Hermes.
- Answer a Jibo conversation or choose native actions.
- Check pairing, authorization, capabilities or reply delivery.

## Prerequisites

Use Hermes 0.21.1 with Python 3.11 and **Agent Mode app 1.0.1 or newer**.
The plugin is tested with Hermes 0.21.1; its manifest accepts that version.
Follow the repository README for the full immutable install pin, dependencies
and exact install/upgrade/remove commands.

Keep Jibo and its awake host on the same trusted home network. The connection
is `ws://<robot-host>:8890/hermes`, with `tls: false`. Plain WebSocket does not
encrypt codes, tokens or conversations. Never expose it or the dashboard to
the Internet, forward router ports or change the robot firewall silently.

## Setup procedure

1. Install and enable the plugin, configure native mode, and open the Hermes
   dashboard in the selected profile. With a named profile, use
   `hermes -p <profile>` for every related command through `terminal`.
2. On Jibo open **Agent Mode → Pairing**. Enter its exact five-character
   transport code in the dashboard's **Jibo** tab. Pairing saves a private
   token and separate robot identity pin; it does not turn Agent Mode on.
3. Start or restart the selected Hermes gateway. Confirm **Host connected**,
   not just **Paired**; the connection needs an active matching native session.
4. Review the Jibo tools in `hermes tools`, including global plugin/MCP and
   provider-added tools. An empty platform list is not a sandbox or a guarantee
   of zero tools. Ambient speech can use the permissions the owner allows.
5. Tap **Enable** on Jibo. If it shows an eight-character **HERMES AUTHORIZATION**
   code, run the exact profile-specific command displayed, using `terminal`:
   `hermes pairing approve jibo <code>`. This is separate from transport pairing.
   Do not broaden all-platform authorization to avoid it.
6. Repeat a harmless spoken request. Check speech and chat, a follow-up, Stop,
   and Off/host-disconnected Classic before marking the device **Tested**.

`jibo-local-user` represents shared local input, not a recognized individual
voice. See [pairing and authorization](references/pairing-and-auth.md) and the
[platform-adapter contract](references/hermes-platform-adapter.md).

## Conversation delivery

Answer ordinary conversations directly in one or two natural sentences. The
native adapter streams, speaks and displays the reply. Use `jibo_speak` only
for an intentional extra utterance or explicit style/safe ESML. After a
successful `final=true`, finish with the same short plain-text answer for
history. Never speak private reasoning, raw tool JSON or operational notes.
See [the response contract](references/channel-response-contract.md).

A new question, Stop or dismissal invalidates earlier outputs; cancelled work
must not borrow the next turn's identity. Cancellation cannot undo an action
already performed. Native delivery owns bounded follow-up listening; do not
add `jibo_listen` after a completed answer merely to be polite.

## Capabilities and pitfalls

- Check the connected robot's advertised capabilities and grants before acting.
  Curated `jibo_express` uses its vocabulary; do not guess expression keys.
  Raw animation tools need separately authorized local catalogs, not bundled
  proprietary data.
- Camera access is off in the Agent Mode app. On a compatible app with explicit
  camera grants, use [native observation](references/native-look-capture.md).
  Inspect a real captured image with `vision_analyze` before describing it;
  check `shown` before claiming visibility. Report stale or missing readings.
- Saved tokens are not a connection test. Old entries without a robot identity
  pin remain unpinned; pairing again adds the pin. Do not bypass a mismatch by
  deleting the identity field.
- A save failure after pairing can be retried in the same dashboard process.
  Its private retry cache is not durable; losing the response or restarting the
  process before saving requires reset and re-pairing.
- Host removal does not unpair Jibo. To reset pairing, uninstall Agent Mode on
  Jibo with **Remove the app and its data**, not
  **Remove the app, keep its settings**, reinstall, then open **Pairing**.
  Do not inspect, copy or manually delete robot token files.

## Verification

Keep **Installed / Paired / Host connected / Mode on / Tested** distinct.
Run source-only tests in an isolated home using the README/CI instructions;
never borrow live configuration or contact a robot or provider for unit tests.
Check real speech, chat, Stop and Classic separately with the owner. Keep the
host awake and check its service startup after sleep, logout or reboot.

Host code is BSD-3-Clause. This guide retains its original Hermes Agent author
and MIT license; see the repository NOTICE for the terms.
