# Planner fixture notes

`valid_plan.json` is one `POST /v1/messages` **response body**, in the shape the
Anthropic SDK (0.121.0) models it: `anthropic.types.Message`, with the plan
itself as the JSON text of a `text` block, because that is where
`output_config.format` puts structured output.

Unlike the `claude-code/` and `codex/` fixtures, this one is **composed, not
captured**. It is written against the SDK's own response types rather than
transcribed from a paid call, and it is stated here rather than implied: the
only thing a live capture would additionally prove is that the API accepts this
module's JSON Schema, and no recording can prove that — it has to be re-checked
against the live API when the schema changes. `tests/orchestrator/test_planner.py`
carries that check as a `harness`-marked test that runs only under
`AGENTHUB_RUN_LIVE_HARNESS=1`.

## What the shape is load-bearing for

- The plan arrives as **one text block containing JSON**, not as a top-level
  field of the response. A `thinking` block precedes it: thinking is adaptive by
  default on `claude-opus-5`, so any code reading `content[0]` gets reasoning
  rather than the plan. The fixture keeps the thinking block for exactly that
  reason.
- `usage` carries all four fields of invariant 3 plus the `cache_creation` TTL
  split. `cache_read_input_tokens` is an order of magnitude larger than
  `input_tokens`, as it is in life once the system prompt is cached; a planner
  that summed `input_tokens` alone would report ~6% of the real spend.
- Field names inside the plan are `design.md` §8's, including `suggested_harness`
  and `suggested_model`. Node ids are planner-local slugs and `depends_on`
  refers to slugs, never to `node_<ULID>`.
- The plan contains a two-parent node (`auth_tests`), so mapping slugs to
  allocated ids has to survive a node with more than one edge.

Variants — a cycle, an orphan `depends_on`, a refusal, a truncation, an API
error — are derived from this body in the test module rather than stored as
separate files: they differ by one field each, and a directory of near-identical
bodies hides which field is the point of each one.

No credential, environment value or absolute path appears in this file.
