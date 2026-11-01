# Design notes

The specification this repository implements is `xys_superagent-架构.md`, delivered
with the task brief (it is not vendored here). This page records only the
places where the implementation had to choose, so the next person does not have
to reverse-engineer the choice from the code.

## What is implemented where

| mechanism | file:line | caller | test that goes red |
|---|---|---|---|
| stage table | `xys_superagent/core/stage.py::stage_of` | `app/loop.py::_settle` and `_dispatch` | `tests/test_c4_stage_table.py` |
| idempotency key | `xys_superagent/core/identity.py::trigger_key` | `app/loop.py::pass_once` | `tests/test_c2_idempotency.py` |
| admission | `xys_superagent/core/admit.py::admit` | `app/loop.py::pass_once` | `tests/test_c7_reject_feedback.py` |
| scope boundary | `xys_superagent/core/ticket.py::Mandate.accepts` | `app/loop.py::_handback` | `tests/test_c6_out_of_scope.py` |
| panel filter | `xys_superagent/core/panel.py::attention_view` | `app/loop.py::_settle` | `tests/test_c5_panel_silence.py` |
| slot mutex | `xys_superagent/core/slot.py::SlotBoard` | `app/loop.py::_dispatch` | `tests/test_heartbeat_slots.py` |
| heartbeat | `xys_superagent/core/heartbeat.py::stalled` | `app/loop.py::_settle` | `tests/test_heartbeat_slots.py` |
| layering | `scripts/check_layering.py` | CI, `doctor` | `tests/test_c1_swap_executor.py`, `tests/test_c8_layering.py` |

## Decisions that the spec left open

**Cursor timing.** The spec's step ⑧ says "persist the cursor before ⑥" while
the numbering puts ⑧ last, and D3 asks for cursor and ticket in one
transaction. The loop commits cursor + triggers + tickets + decisions in a
single `pass` record and only then dispatches. Consequence: a crash can replay
an event (at-least-once), never dispatch work the ledger does not know about.
This is the trade-off §7.3 describes, made literal.

**A pass record, not a widened port.** "Cursor and ticket in one transaction"
is expressed by one `Record(kind="pass")` whose payload carries the new cursor
and the rows produced in that pass. The SQLite adapter decomposes it inside one
`BEGIN IMMEDIATE` transaction. The port keeps its three methods.

**Two small port extensions.** `SourcePort.reply(trigger, note)` exists because
§8.2 requires a refusal to return to the conversation, and there is no other
channel; `Message(intent="probe")` exists because §7.1⑥ requires collecting
executor reports through the same port as deliveries. Both are documented at
the point of definition.

**Empty scope.** `""` is rejected everywhere; `"."` is the whole workspace;
an empty `scope_paths` tuple denies every action. `admit` rejects an explicitly
empty scope entry instead of guessing, and the build refuses an empty
`default_scope_paths`.

**`stalled` is a report, not a stage.** The seven-row table has no `stalled`
row, so a lost heartbeat is recorded as a `blocked` report and therefore shows
up in the four waiting stages. The heartbeat is seeded from the persisted
ticket (`heartbeat_at`), so a restart does not immediately declare a stall.

**Dispatch after a crash.** Before `hand_off` the loop cannot know whether a
previous process already dispatched. The work reference carries a stable
`dispatch_key` (`ticket-id:round`), which the bridge uses as its `messageId`;
the bridge deduplicates on it, so the retry is safe. An executor without
request deduplication makes this at-least-once at the executor boundary, which
is stated in the README rather than hidden.

**A handback ends the attempt.** After `out_of_scope` the ticket becomes
`blocked`; it is not left merely "unassigned", because that would put it back
in the queue and dispatch it again in the next pass.

**Ledger on a Windows drive.** SQLite WAL can be refused by the filesystem; the
adapter says so on stderr and falls back to the rollback journal, and `doctor`
recommends a local ext4 path. A lock file (`<database>.lock`) makes a second
writer fail immediately instead of corrupting the ledger.

**Reading executor state.** A2A `tasks/get` is asked with `historyLength=0`.
Without it the bridge returns the whole conversation, which was measured in
the thousands of messages for a single task and cannot fit a two-second pass.

## Known limits

* Only one process may own a ledger (enforced by the lock file). Multi-machine
  deployment is explicitly out of scope.
* A ticket never closes itself; `ready` stops at `waiting_review` until a
  person acts. There is no "accept" command yet.
* The GitHub source reads issues for one repository, or the account's
  notifications (the latter without an author, so admission conservatively
  refuses it).
* `exec_contactor` targets the A2A v0.3 JSON-RPC binding (`message/send`,
  `tasks/get`, `tasks/cancel`). A v1.0 bridge renames the methods and the
  state enum; the adapter would need a version branch.
