# Changelog

## 0.3.1

- Clarified setup for Agent Mode app 1.0.1 or newer over a trusted home network.
- Made pairing and connection states distinct in the Jibo dashboard, with
  clearer restart guidance and a separate Hermes authorization screen.
- Reject malformed pairing codes and invalid ports without consuming a code.
- Pin the robot identity separately from the local device alias; refuse a
  connection that advertises a different robot or an inactive/wrong session.
- Check configuration writeability before pairing and verify private saves.
  Retain received pairing results in the running dashboard for a safe save retry.
- Preserve existing device, model/provider, session and timeout settings when
  saving or pairing again.
- Avoid exposing pairing tokens in configuration previews and error messages.
- Align plugin, dashboard and asset cache versions to 0.3.1.
- Document pinned installation, upgrade, removal, data-erasing pairing reset,
  tool permissions and the unencrypted home-network connection.
