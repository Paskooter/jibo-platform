# jibo-platform for Hermes

Connect Jibo's **Agent Mode** app to your Hermes agent. This Python gateway
plugin adds a **Jibo** tab to the Hermes dashboard, pairs your robot, and sends
conversation replies to its speech and chat display. Hermes runs on your
computer and uses your configured model, provider, tools and conversation
history. No extra companion process or robot firmware installation is needed.

Host code is [BSD-3-Clause](LICENSE); the bundled agent guide is MIT-licensed.
See [NOTICE](NOTICE) and [CHANGELOG.md](CHANGELOG.md).

## Requirements

- **Hermes 0.21.1** with a working model/provider. The minimum version is **0.21.1**;
  this plugin release's manifest accepts only that version.
- **Python 3.11**, Git, and an awake computer running the Hermes gateway.
- **Agent Mode app 1.0.1 or newer** installed on Jibo. This app version accepts
  home-network connections; update older Agent Mode apps before connecting.
- Jibo and the computer on the **same trusted home network**.

Tested with Hermes 0.21.1 at public source commit
[`6494163911460844c73ac76abb5aaa4f967ae14b`](https://github.com/NousResearch/hermes-agent/commit/6494163911460844c73ac76abb5aaa4f967ae14b),
Python 3.11.15 and Linux. The tests use Node 22.22.0 for dashboard checks;
Node is not a runtime companion for the plugin.

First complete [Hermes installation][installation], then check the provider:

```sh
hermes setup
hermes doctor
hermes chat -q "Say hello in one sentence."
```

**Home networks only — never expose it to the Internet.** The native connection
uses unencrypted `ws://` on port `8890`, path `/hermes`. Someone who can inspect
network traffic can see codes, tokens and conversations; a captured token can
authorize robot control. Never forward router ports or expose the robot bridge
or Hermes dashboard publicly. Pairing is authentication, not encryption. A
robot's firewall-off setting opens all ports, not just `8890`; this plugin does
not change firewall settings. See [SECURITY.md](SECURITY.md).

## Install

Review the source and Hermes's installation scan before enabling. The plugin
runs in the Hermes process with your computer user's privileges, not in a
sandbox. Gateway adapters use Hermes's in-process plugin mode.

Hermes 0.21.1 prints dependency instructions but does not install plugin Python
packages automatically. For a standard Hermes source installation, use its
managed uv to prepare the same Python environment (adjust these paths if your
Hermes installation is elsewhere). This also works when that environment has
no pip module:

```sh
"$HOME/.hermes/bin/uv" pip install --python "$HOME/.hermes/hermes-agent/venv/bin/python" 'httpx==0.28.1' 'websockets==15.0.1' 'PyYAML==6.0.3' 'fastapi==0.133.1' 'Pillow==12.3.0' 'aiohttp==3.14.3'
"$HOME/.hermes/bin/uv" pip check --python "$HOME/.hermes/hermes-agent/venv/bin/python"
```

Install the plugin at its full, immutable **40-character commit SHA**
([Hermes plugin documentation][plugins]):

```sh
hermes plugins install https://github.com/Paskooter/jibo-platform --ref fa793e41148b3ebdd41a5543732deba7217de146 --enable
hermes plugins list
```

HTTP/WebSocket clients use httpx and websockets, dashboard settings use PyYAML
and FastAPI, image normalization uses Pillow, and the optional turn-ingest
listener uses aiohttp. No robot SDK, raw catalog or firmware checkout is needed.

### Use one Hermes profile

Commands here use the default profile. With a named profile, insert
`-p <profile>` after `hermes` in **every** plugin, configuration, tools,
dashboard, gateway and authorization command. To create a separate blank
profile, use `hermes profile create jibo`, then `hermes -p jibo setup`.
Plugin installation, configuration and authorization belong to that profile.

## Pair Jibo

Enable the native platform and open the dashboard:

```sh
hermes config set gateway.platforms.jibo.enabled true
hermes config set gateway.platforms.jibo.extra.mode native
hermes dashboard
```

1. On Jibo, open **Agent Mode → Pairing**. Read the **five-character transport
   code**, which expires after 15 minutes.
2. In the Hermes dashboard's **Jibo** tab, enter a device alias and display name,
   Jibo's local hostname or IP, port `8890`, path `/hermes`, and `tls: false`.
   Enter all five code characters exactly, without spaces or punctuation.
3. Pair the device. The dashboard saves the transport token privately in your
   selected Hermes profile and pins the identity returned by Jibo separately
   from your local alias. Do not copy tokens into commands, photos or issues.
4. Start the gateway and check status:

```sh
hermes gateway install
hermes gateway start
hermes gateway status
```

If your gateway is already running, use `hermes gateway restart` after pairing
or changing devices. Restarting interrupts other platforms in the **same**
gateway briefly. Keep only one active controller connected to each Jibo.

The dashboard distinguishes **Installed**, **Paired**, **Host connected**,
**Mode on**, and **Tested**. Saving a token does not mean the gateway has
connected, and pairing does not turn Agent Mode on. **Host connected** requires
authentication and an active matching native session.

## Review tools and authorize conversation

Use `hermes tools` and select the **Jibo** platform to review the tools you want
the agent to use. Include globally enabled plugin/MCP tools and provider-added
tools in that review: an empty `platform_toolsets.jibo: []` alone does **not**
disable every tool. Ambient speech can request anything your allowed tools
permit. The plugin does not offer read-only sandbox tiers or per-action robot
approval. Do not turn on all-platform authorization to simplify setup.

On Jibo, tap **Enable**. Your first spoken request can display a separate
**eight-character HERMES AUTHORIZATION code**. Approve it on your computer using
the exact command shown on Jibo, in the same Hermes profile:

```text
hermes pairing approve jibo <eight-character-Hermes-authorization-code>
```

This is **not** the five-character transport code. Hermes authorizes the shared
local-input identity `jibo-local-user`; it is not voice recognition or proof of
who spoke. Do not set `GATEWAY_ALLOW_ALL_USERS=true` to bypass approval. See the
[pairing reference](agent-skills/jibo-hermes-integration/references/pairing-and-auth.md).

Repeat a harmless request after approval. Check that the answer is both audible
and displayed, try a follow-up and **Stop**, then turn Agent Mode Off or say
**“go back to classic”**. Classic requests continue with Agent Mode Off or the
host disconnected. Keep the host and gateway awake for Agent conversations.

Hermes uses its normal model/provider unless you select a device override in
the Jibo tab. Ordinary replies stream, speak and appear in chat automatically;
`jibo_speak` is for an intentional extra utterance or speech style, not every
answer. The bundled guide is available through
`skill_view("jibo-platform:jibo-hermes-integration")`.

## Troubleshooting

- **Connection refused:** check Jibo's address, port and network. Update an older
  Agent Mode app to 1.0.1 or newer; a firewall change does not repair an app
  that accepts only local-loopback connections.
- **Paired but disconnected / restart required:** restart the selected gateway
  once, check `hermes gateway status`, and confirm no other controller is using
  Jibo. A connection made before a new pairing is not the new pairing's success.
- **Identity mismatch:** verify the address points to the intended robot. Do not
  remove its saved identity pin to bypass the mismatch; reset and pair the
  intended robot again.
- **Pairing received, save not verified:** keep the dashboard process running,
  fix the configuration write problem, and retry with the same alias, address
  and code. It retains the result privately in memory without consuming another
  code. This recovery ends when the dashboard process exits.
- **Code consumed, response lost or dashboard restarted before saving:** reset
  pairing using the procedure below, then pair again. A genuinely lost one-shot
  response cannot be recovered by retrying the consumed code.
- **No provider reply:** try an ordinary `hermes chat` request first; check model
  and provider settings independently of the robot connection.
- **Older saved device:** existing tokens without a robot identity pin remain
  usable and appear as unpinned legacy entries. Pair again to add the identity
  pin; editing the alias does not add one.

### Reset pairing

Removing the host plugin **does not unpair Jibo**. To erase the robot's pairing,
uninstall **Agent Mode** on Jibo and choose **Remove the app and its data**,
not **Remove the app, keep its settings**.
Reinstall the app, open **Agent Mode → Pairing**, and pair again. Erasing app
data also clears that app's saved settings. If your app manager lacks the
erase-data choice, update it before resetting. Do not inspect, delete or copy
robot token files. Confirm Jibo offers a new pairing code before transferring
it to another owner.

## Upgrade, disable or remove

Pinned installs do not move with `hermes plugins update`. Review a new release,
then use its full 40-character commit SHA in this command:

```text
hermes plugins install https://github.com/Paskooter/jibo-platform --force --ref <new-full-40-character-commit-SHA> --enable
hermes plugins list
hermes gateway restart
```

Keep your selected profile, device configuration and model choices. Review
changed dependencies and permissions instead of automatically following a
branch or tag.

To disable and remove the host plugin:

```sh
hermes config set gateway.platforms.jibo.enabled false
hermes plugins disable jibo-platform
hermes gateway restart
hermes plugins remove jibo-platform
```

Removing a dashboard device also removes only the host configuration; restart
the gateway afterward. Neither action revokes Jibo's saved token. Use **Reset
pairing** above when revocation is intended.

## Limitations

- The home-network WebSocket is unencrypted; it is not suitable for public or
  untrusted networks.
- Tool selection is not a sandbox, and local-user authorization does not
  distinguish individual voices.
- Camera access is off in the Agent Mode app. Camera, observation and Web Panel
  actions require capabilities and grants advertised by a compatible app.
- Curated `jibo_express` uses the robot's advertised vocabulary. Raw animation
  catalogs are not bundled; `jibo_find_expression` and `jibo_play_expression`
  are unavailable without separately authorized local catalog data.
- Pairing retry is process-memory-only, not crash recovery. Host service,
  sleep, logout and reboot behavior depends on your Hermes installation.

## Development tests

CI uses Python 3.11, Node 22.22.0, the Hermes commit listed above and the six
manifest dependencies. Install `pytest==9.1.1` in that same environment, then
run from a clean checkout:

```sh
PYTHONPATH="$PWD/tests${PYTHONPATH:+:$PYTHONPATH}" JIBO_RELEASE_INTEGRATED=1 python -m pytest tests -q
JIBO_RELEASE_INTEGRATED=1 python -m unittest tests.test_public_release.PublicArtifactTests -v
```

Tests use synthetic peers and temporary profiles, never a robot, provider
credential, private catalog or firmware checkout. Artifact checks enforce the
source allowlist, license notices, install pin and matching dashboard cache
version. The installer test verifies an exact local Git pin in an empty Hermes
home. See [.github/workflows/ci.yml](.github/workflows/ci.yml) for isolated setup.

[plugins]: https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins
[installation]: https://hermes-agent.nousresearch.com/docs/getting-started/installation
