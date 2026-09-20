# The anti-false-positive method

This is the reasoning veripipe is built on. It is written to stand on its own:
you can apply the method by hand, or with any tool, not just this one.

## The problem

An agent that tests software is good at *finding* candidate defects and weak at
*proving* them. It will produce a fluent, confident report for behaviour that is
actually correct. Those false positives are expensive: a human spends real time
disproving a machine's guess, and after a few of them the human stops trusting
the machine at all. The goal is not to find more candidates. It is to let
through only the ones backed by evidence.

## One rule

Confirmation comes from deterministic, structured evidence. The agent's prose is
never evidence. "The page shows an error" is a sentence, not a defect.

Everything below follows from that rule.

## The oracle ladder (L1–L3, and why L4 is quarantined)

Grade a finding by the strength of the check that judged it:

- **L1 — structural.** The system violated a hard invariant: a crash, a 5xx, a
  malformed response, a broken schema. Cheap and unambiguous.
- **L2 — terminal state.** After the action, the *machine-comparable* end state
  does not match the contract: a field, a count, a mode. Compared against a
  semantic map, not against rendered text.
- **L3 — reproduction / relational.** The behaviour is confirmed by repetition
  or by a relation between runs (differential, metamorphic): same input, same
  outcome; equivalent inputs, equivalent outcomes.
- **L4 — heuristic.** "This looks wrong." A hunch, a similarity, a model's
  opinion. Useful as a *lead*, never as a verdict. L4 is isolated in its own
  channel and can never become a filed report on its own.

Only L1–L3 may be filed. If you cannot state the check as a mechanical
comparison, you are at L4.

## The model may veto, never confirm

When a language model is in the loop, restrict it to three outputs: `allow`,
`veto`, `request-more-evidence`. It can throw a candidate out or ask for more,
but it can never be the reason a finding is confirmed. Confirmation always comes
from a deterministic check. This asymmetry is deliberate: a model asked "is this
a bug?" will agree with itself, so you never let it answer that question.

## Fail closed

Every ambiguous outcome resolves to "do not file": missing evidence, an
assertion type the oracle does not recognise, a semantic map that has drifted
from the product, a telemetry gap, an unknown verdict, an exhausted budget. The
default is silence, not a report. A verifier that guesses when unsure is just
another source of false positives.

## Structured terminal state, not text

An L2 check needs a *machine-comparable* terminal state and a contract for it —
for example `current_mode == target_mode`, `pending_count == 0`, or
`goal_id present`. The contract lives in a semantic map, separate from the code
that runs the agent. The oracle reads the observed structured state and compares
it. It does not read, summarise, or pattern-match the agent's output. If the
only thing you can observe is text, the honest verdict is `inconclusive`.

## Contracts must describe the product, not the tool

The most common way to turn a verifier into a noise machine is to write a
contract the product never promised. If an agent legitimately leaves a task
`in_progress`, a contract that demands `completed` will "find" a defect that is
not there. Write contracts that reflect the product's *real* expected terminal
states. A false contract produces false failures, which is the exact thing you
set out to prevent.

## Keep the engine product-neutral

The judging engine should contain no product names, endpoints, credentials, or
domain knowledge. Product specifics — the semantic map, the transport, the
routes — live in a separate adapter, kept private. This keeps the method
portable across products and keeps proprietary knowledge out of a shared engine.
Only structured observed-state crosses the boundary into the engine.

## Publishing is low-noise by construction

A verification tool that spams is self-refuting. So filing is:

- **off by default** — a run produces *drafts*, not posts; a human decides;
- **verified only** — L1–L3 with no model confirmation; L4 and prose never file;
- **deduplicated and capped** — one fingerprinted report per distinct finding;
- **transparent** — every filed item says it was produced by a verifier and how
  to reproduce it.

Restraint is not a limitation here. For a tool whose whole claim is "less noise,"
restraint *is* the product.

## Worked examples

Two runs, wired to a real agent harness:

1. An agent was asked to create a three-step todo list and complete it. It wrote
   three items and its reply read as finished, but the structured state still
   had one item pending. The L2 check `pending_count == 0` failed, and a
   fingerprinted draft was produced for review — not a pass on the agent's word.

2. An agent was asked to say, in text, that a goal had been created, and to call
   no tool. It reported success. There was no tool call and no structured state,
   so the check returned `inconclusive` and filed nothing. The claim was not
   confirmed, because nothing deterministic backed it.

The first shows the method catching a false "done." The second shows it refusing
to confirm an unbacked claim. Both come from the same rule: evidence decides,
prose does not.

## What the method deliberately does not do

- It does not compare agent text or rendered output.
- It does not let a model confirm a finding.
- It does not file when unsure.
- It does not post anywhere by itself.

## Applying it to a new product

1. List the behaviours worth checking and, for each, the *real* machine-
   comparable terminal state the product promises.
2. Write those as contracts in a semantic map (with an explicit `machine_check`).
3. Provide a transport that drives the product and returns structured state.
4. Run candidates through the ladder; let only L1–L3 with real evidence through.
5. Stage drafts, review, and file sparingly — into the right place.
