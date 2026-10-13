# Native observation (optional capability)

**Camera is off for the current Agent Mode app descriptor.** Do not offer a
room scan or photo as part of first-run setup. The host implementation does not
create permission. These notes apply only to an independently compatible app
that advertises and grants the existing camera/observation capabilities.

Use `jibo_observe(direction="left")`, `center`, `right`, `far_left`,
`far_right`, `up` or `down` only when supported. Omit direction to retain the
current gaze, or provide `position: {x, y, z}` in robot-relative metres. The
native observation operation combines confirmed movement, an attention hold
and capture. Do not open a second controller or use an excluded timing script.
Concurrent actions can return `ROBOT_BUSY`; perform one view at a time. A failed
move is not a successfully aimed photo, and Stop/disconnect ends in-flight work.

On successful authorized capture, the host result contains `image.local_path`,
URL, capture time and `image.shown`. If `image.vision_attached` is true, inspect
that attached image. Otherwise follow `next_step` and inspect the saved image
with `vision_analyze` before describing it. Check `shown` before claiming it
appears in chat; do not invent a picture or silently substitute an unrelated
camera. Do not log native tokens or expose household photos without consent.

`jibo_observe(capture=false)` requests sensors without a photo or movement,
subject to the connected app's capabilities. Presence can be true, false or
null (not measured). Respect supplied ages/freshness; do not use absent/stale pose
vectors as current measurements. Robot and host clocks may differ. Hardware
temperatures are not room temperature. Do not infer depth, ambient temperature
or sound-source direction from fields that do not provide them.
