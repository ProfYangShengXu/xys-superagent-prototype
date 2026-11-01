# xys-superagent-prototype

**Tickets and a ledger for work that is handed to agents.** One process, one
SQLite file, no external services. The executor is swappable; the core does not
move.

`contactor` is the hands and feet: it carries a message to an agent.
`xys_superagent` is the ticket and the ledger: it remembers what was asked, what
is in scope, what stage the work is at, and who is waiting for a person.

> **Status: research prototype, not production software.** 110 tests and one
> runnable example; read the code before you rely on it. The problem it solves
> is narrow — one machine, one team, a handful of agents — and the trade-offs
> here do not transfer unchanged to another scale.

Three incidents motivate every line in this repository, because all three
happened before it existed:

| incident | the structural answer |
|---|---|
| the same job was dispatched twice after a bridge restart | an idempotency key per event plus a cursor committed in the same transaction as the ticket |
| a 40-minute compile was judged dead because no file changed | stage is computed from executor reports and a heartbeat, never from file timestamps |
| an agent changed one more file than it was asked to | the ticket carries a `Mandate`; anything outside `scope_paths` can only be handed back |

## What this is not

```
✗ not an agent framework   no model calls, no tool surface, no opinion on how an agent thinks
✗ not a scheduler         no optimal assignment; serialising per workspace is enough
✗ not a job queue         no retry storms, no priorities; retries come from executor reports
✗ not multi-tenant        one ledger, one writer
```

## Run it

```bash
# 1. four passes against the bundled example (one pass cannot finish a job)
python3 -m xys_superagent.app.cli run  --config examples/config.toml --max-passes 4

# 2. what is waiting for a person (starts empty, then shows the two reviews)
python3 -m xys_superagent.app.cli view --config examples/config.toml

# 3. the environment and the wiring, one line per check
python3 -m xys_superagent.app.cli doctor --config examples/config.toml
```

`run` is a single-threaded loop: one pass, then the next, never two at once.
`view`, `ticket` and `doctor` open the ledger without starting the loop, so
they work while an executor or an event source is unavailable.

If you have a coding agent at hand, hand it this README and let it run `doctor`
first: every line says what that item is worth and what to do about it, so the
agent does not have to guess. 中文说明见 [README.zh-CN.md](README.zh-CN.md)。

## Layout

```
xys_superagent/
  core/       domain: stage table, admission, identity, ticket+mandate,
              panel filter, workspace slots, heartbeat.  Standard library only.
  ports/      four narrow protocols: SourcePort, ExecutorPort, StorePort,
              PanelPort.  No transport words in them.
  adapters/   sqlite ledger, JSONL file source, GitHub source,
              bridge executor (A2A v0.3 JSON-RPC), command-line executor,
              plain-text panel.
  app/        build_app() is the only assembly point; loop.py is the 8-step
              pass; cli.py is the entry point.
```

Dependency direction is enforced by `scripts/check_layering.py`, which parses
the import graph and fails on a reverse dependency, a third-party import
outside `app/`, or a cycle. It runs in the test suite and from `doctor`.

## Three guarantees, and how they are tested

| # | guarantee | test that fails if it is broken |
|---|---|---|
| C1 | swapping the executor does not touch `core/` or `ports/` | `test_c1_swap_executor.py` |
| C2 | the same event delivered twice creates one ticket | `test_c2_idempotency.py` |
| C3 | a restart does not replay consumed events | `test_c3_restart_cursor.py` |
| C4 | the stage table is first-match-wins, 12 fixtures | `test_c4_stage_table.py` |
| C5 | a healthy ticket takes zero bytes on the panel | `test_c5_panel_silence.py` |
| C6 | an out-of-scope request is handed back, never widened | `test_c6_out_of_scope.py` |
| C7 | every refusal carries a reason and returns to the source | `test_c7_reject_feedback.py` |
| C8 | the import graph has no cycles and no reverse dependencies | `test_c8_layering.py` |

Each test file contains a positive case **and** a counter-example that
deliberately breaks the mechanism and asserts the check would notice. Run them
with an environment that has `pytest` installed (the package itself has no
third-party dependencies):

```bash
python3 -m pytest
python3 scripts/check_layering.py
```

## Adding another agent framework

Implement the three-method executor protocol. This is the whole adapter; the
core and the ports do not change.

```python
# xys_superagent/adapters/exec_myframework.py
from ..core.types import Assignment, Delivery, Message, Report, WorkRef
from ..core.ticket import Mandate

class MyFrameworkExecutor:
    def __init__(self, url: str) -> None:
        self.url = url

    def hand_off(self, mandate: Mandate, work: WorkRef) -> Assignment:
        return str(self._post("/start", {
            "workspace": work.workspace,
            "scope": list(mandate.scope_paths),
            "rounds": mandate.max_rounds,
        })["handle"])

    def deliver(self, assignment: Assignment, message: Message) -> Delivery:
        if message.intent == "probe":
            state = self._get(f"/state/{assignment}")
            return Delivery(ok=True, report=Report(status=state["status"]))
        self._post(f"/send/{assignment}", {"text": message.text})
        return Delivery(ok=True, note="sent")

    def stop(self, assignment: Assignment, reason: str) -> None:
        self._post(f"/stop/{assignment}", {"reason": reason})
```

Then name it in the only assembly point, `xys_superagent/app/build.py`:

```python
elif kind == "myframework":
    return MyFrameworkExecutor(_str(data, "url"))
```

Run `python3 scripts/check_layering.py`; it should still say
`no cycles, no reverse dependencies`.

## Configuration

```toml
[app]                       # ledger path, poll interval, heartbeat threshold, retry base
[policy]                    # who may start work, what actions are allowed, default scope
[sources.<name>]            # kind = "file" | "github"
[executor]                  # kind = "cli" | "contactor"
[panel]                     # kind = "text"
```

A source is either a JSONL file (used by the example and by tests) or GitHub
issues/notifications through the `gh` command line. A missing `gh` raises
`GhMissing` with install instructions; it is never reported as "no events".
The bridge executor raises `BridgeUnreachable` with the address and a concrete
next step. From inside WSL, `127.0.0.1` is the WSL loopback: a bridge bound to
`127.0.0.1` on the Windows host must be reached through the host address (or
WSL mirrored networking), and `doctor` says so when the probe fails.

## Design notes

* The ledger is the only truth. Slots are derived from ticket state, so a
  restart cannot leak a slot; stages are recomputed, never remembered as flags.
* The pass commits the cursor and the tickets it produced in one SQLite
  transaction, **before** anything is handed to an executor. A crash can replay
  an event; it cannot dispatch work the ledger does not know about. The
  executor receives a stable `dispatch_key`, so a retry is deduplicated by a
  bridge that keys on it.
* `stalled` means "the heartbeat stopped", not "nothing changed on disk". A
  lost heartbeat becomes a `blocked` report, which is a stage a person sees.
* `mandate.scope_paths` is checked structurally: `""` is rejected, `"."` is the
  whole workspace, `..` that escapes the root is rejected, and `src/ab` is not
  inside `src/a`.
* Single writer: a lock file next to the database makes a second process fail
  fast instead of corrupting the ledger.

See `docs/design.md` for where this implementation departs from the written
spec and why.

## Docs

Read in this order; each one only uses what the ones before it introduced.

```
README.md              this file
README.zh-CN.md        中文版（同样的内容，按中文习惯重写，不是逐句直译）
docs/architecture.zh-CN.md  the design document this was built from (Chinese)
docs/design.md         where this implementation departs from that spec, and why
examples/config.toml   the smallest configuration that runs
examples/events.jsonl  two example events
```

`docs/architecture.zh-CN.md` is the design document this implementation was
built from: the change-frequency layering, the four ports, the stage table, the
acceptance criteria C1–C8, and where this prototype deliberately stops short of
the reference one. It is in Chinese, and it is the one page worth reading before
changing `core/`.

## License

MIT. See `LICENSE`.
