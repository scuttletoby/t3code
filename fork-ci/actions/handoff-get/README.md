# handoff-get

The consumer half of the hand-off pair: it takes the file a [handoff-put](../handoff-put/README.md) of the same run
handed off for this checkout's HEAD, and writes its payload to `path` only when the `sha` inside the file is this
checkout's HEAD (the artifact's name alone is not trusted). A missing or refused hand-off is `ok` `'false'` and no file
at `path`, never a failed step.

Usage, inputs, outputs, how the consumer picks its runner from the producer's `ok`, the state cases and what a pull
request can and cannot do with a hand-off: [handoff-put's README](../handoff-put/README.md). Its code is
`../handoff-put/handoff.py`, its tests `../handoff-put/handoff.test.py`.
