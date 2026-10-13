# Jibo ↔ Hermes pairing and authorization

## Two separate codes

1. On **Agent Mode → Pairing**, Jibo presents a **five-character transport
   code**, expiring after 15 minutes. Enter it exactly in the Hermes dashboard's
   **Jibo** tab with the robot's home-network address, port `8890` and `/hermes`
   path. The exchange saves a private token and a separate robot identity pin.
   Pairing does not turn Agent Mode on.
2. A first spoken request can display an **eight-character HERMES AUTHORIZATION
   code** unless an existing allowlist already authorizes local input. Approve
   it on the computer in the same profile, then repeat the request:

```text
hermes pairing approve jibo <eight-character-Hermes-authorization-code>
```

Use the exact command Jibo displays. With a named profile, use
`hermes -p <profile>` for authorization and every related plugin, configuration,
dashboard and gateway command. Transport pairing, user authorization and an
app-store installation code are different operations.

## Identity and tool access

Native input uses:

```text
platform: jibo
user_id: jibo-local-user
user_name: Jibo user
chat_id: jibo:<configured device alias>
```

Approving this identity authorizes shared local input; it is not voice
recognition or proof of one speaker. Robot identity pinning is separate: new
pairings persist `robot_device_id`, while `id` remains your local alias. A
different advertised robot identity prevents the session from opening. Existing
tokens without a pin remain usable and are labeled unpinned legacy entries;
pair again to add a pin.

Review the effective Jibo tools, including global plugin/MCP and provider-added
tools. An empty platform list does not guarantee zero tools. The plugin is not
a sandbox and does not require per-action robot approval. Do not set
`GATEWAY_ALLOW_ALL_USERS=true` to bypass authorization, hand-edit approval
stores or publish codes/tokens. Expired Hermes codes need a new request.

## Save retry, reset and removal

The dashboard verifies its configuration writer before consuming a transport
code and reads back a private atomic save. If an exchange succeeds but saving
fails, keep the dashboard process running, fix the write problem and retry with
the same alias, address and code. The received token stays in process memory
for that retry and is not returned to the browser.

A genuinely lost response or a process exit before saving requires pairing
reset. On Jibo, uninstall Agent Mode with **Remove the app and its data**, not
**Remove the app, keep its settings**, then reinstall and open
**Agent Mode → Pairing**. Update an app manager that
lacks the erase-data choice before resetting. Do not inspect, copy or manually
delete robot token files. Erasing app data also clears the app's saved settings.

Removing a host device or plugin does not unpair Jibo or revoke its token.
Confirm a new pairing code appears before transferring the robot.

## Check each state independently

- **Installed:** app and host plugin are present.
- **Paired:** transport token saved, not necessarily connected or enabled.
- **Host connected:** token authenticated and an active matching session opened.
- **Mode on:** a local choice on Jibo.
- **Tested:** a spoken provider answer, Stop and Off/host-disconnected Classic work.

Keep the host and gateway awake. Source tests do not exercise physical speech,
touch or microphone behavior; check those with the owner during setup.
