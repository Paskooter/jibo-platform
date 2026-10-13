# Jibo channel response contract

## Ordinary replies

Return a concise, non-empty assistant answer directly. The native adapter
streams the text, starts sentence speech, and records the completed exchange.
No `jibo_speak` call is required. Mandatory speech-tool calls add another model
round trip and delay the final transcript.

Keep raw tool JSON, private reasoning, operational notes, and URLs out of
speech. Obtain live values with a relevant tool before answering; do not invent
a value to make the response faster.

## Intentional speech tools

Use `jibo_speak` when an explicit style/ESML or deliberate spoken progress
update is useful. `final=false` is a progress utterance; the answer can follow.
`final=true` declares that the complete answer has been delivered. After that,
finish with the same short plain-text answer for Hermes' canonical history.
Do not call a second speech tool or start unrelated work after declaring final.

On successful final speech the native adapter publishes the visible answer
immediately. It suppresses later draft speech and progress labels for that
turn until the agent finishes. Suppression belongs to the turn, so slow model
bookkeeping cannot expire it. The next question gets independent ownership.
A failed or stopped speech operation is not marked as delivered and must not
be described as a successful utterance.

If the user reports an empty response, restate the intended answer without
repeating unrelated side effects. Check speech and transcript delivery
separately; a tool's successful delivery does not itself establish that the
canonical saved assistant message was non-empty.
