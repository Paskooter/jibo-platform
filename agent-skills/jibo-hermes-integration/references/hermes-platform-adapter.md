# Hermes platform-adapter contract

The installed `jibo-platform` registers with `ctx.register_platform()` and
implements connect/disconnect, send/draft delivery, and inbound
`handle_message(event)`. Keep changes in the plugin rather than adding a
second agent or conversation store inside the robot.

The native configuration uses per-device ids, hosts, bridge ports, tokens,
and optional model overrides. `jibo:<configured id>` is the chat identity.
The gateway owns the agent and canonical history; the plugin projects a
bounded recent conversation to Be and records locally handled turns there too.

Registration can succeed while a robot is offline. Native readiness requires
authentication and `session.open`; reconnect loops continue independently.
Keep platform health distinct from device readiness.

Worker context captures the original turn id. Native mutations carry
`owner_turn_id`; `speech.stop` also carries its stop-specific `turn_id`.
Cancel agent work and the base adapter's processing task/guard before starting
a replacement turn. Do not clear the replacement's state from old callbacks.

Use ordinary assistant replies for automatic speech/display. An optional
`jibo_speak` call is for intentional progress or style/ESML; see
[channel-response-contract.md](channel-response-contract.md).
