# Architecture and evidence boundaries

## Components

### Python controller

The Python package owns planning, phase progression, survival gates,
checkpoint state, transport abstraction, and observed-effect verification.
Policies are separated from transport mechanics so the same controller logic
can be exercised with deterministic fakes.

### Fabric bridge

The Java bridge runs inside the Minecraft client. It validates and dispatches
commands, publishes events, correlates responses with sequence numbers, and
records uncertain mutations. TCP, WebSocket, and MCP-facing integrations share
contracts without pretending their delivery behavior is identical.

### Dashboard

The React frontend presents controller and bridge observations. It is an
operational view, not an independent source of truth.

### Test layers

Offline tests cover state machines, contracts, retries, reconciliation, and
safety gates. Functional harnesses require an explicitly selected disposable
or read-only world. A bridge build proves compilation; a functional response
proves only the behavior that was actually observed.

## Evidence ladder

| Level | Evidence | What it proves |
| --- | --- | --- |
| 1 | Controller dispatch | The controller attempted an action |
| 2 | Bridge response | The bridge accepted or rejected it |
| 3 | Completion signal | The requested operation reached a terminal state |
| 4 | World observation | The expected block, inventory, position, entity, or health change occurred |
| 5 | Durable state | The verified result survived checkpointing, recovery, and later progression |

Higher levels include the lower context but cannot be inferred from them. A
successful response is therefore never reported as a durable gameplay result
without the corresponding observation.

## Unattended recovery evidence

Successful cycle counts and escaping nearby threats are activity, not resource
production. The work ledger credits observed output counters and positive
resource/equipment deltas. Per-kind zero-output streaks survive checkpoint
reloads and unrelated successful work. Repeated production attempts back off
up to fifteen minutes; emergency food, defense, and equipment retain their
normal admission rules.

Mine preparation retrieves local stored torches, fuel, and sticks before wood
gathering. Its charcoal fallback harvests one locally observed natural trunk
in daylight with protected travel and fresh inventory evidence. Rejected trees
cool down; available saplings are replanted and a home return is attempted.
A deep saved route stays intact when lighting prerequisites fail.
Food attempts also make a bounded protected return after zero output or a
failed expansion; a separate fresh XYZ/grounded check records the return.
Verified tunnel travel checkpoints the route periodically without crediting
production, and long returns have a bounded budget scaled to the checked path.
Mining preparation requires ten observed free slots, rather than merely the
mid-trip minimum. If local storage cannot make room, a daylight, threat-checked
batch may supply one chest in an observed empty interior house corner. The
chest and resulting free inventory space are verified independently; failed
preparation retries after a short delay without descending the saved mine.
Storage interaction clears only incidental ground cover; walls, floors and
workstations are preserved when they obstruct a container face.

These contracts require offline regression tests and live acceptance separately.
An unattended acceptance run must demonstrate net food reserve growth,
replenished equipment/resources, verified returns, and a new objective over a
full day without operator repair. Heartbeats and harvest-only loops cannot
establish that acceptance.

## Uncertain mutations

Read operations may be retried within a bound. Mutating operations require
idempotency, correlation, or post-timeout reconciliation before any replay.
The mutation ledger and observed-effect helpers make that decision explicit.

## Public/private split

The public repository contains reproducible source, generic fixtures, and
test contracts. Environment-specific deployment and live-world state remain
outside the source tree. Private operations pin the exact public commit they
deploy rather than maintaining a divergent copy of the core.
