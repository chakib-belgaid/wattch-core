# 0001 — Protocol-first foundation

## Status

Accepted.

## Context

Wattch needs to support privileged acquisition daemons and unprivileged clients.

The project should eventually allow pyJoules and JouleHunter to use Wattch Core without requiring normal Python users to read RAPL counters directly.

## Decision

Start with protocol, shared source descriptors, deterministic daemon, and CLI skeleton.

Do not start with real RAPL.

## Consequences

Positive:

```text
hardware-independent validation
clean daemon/client boundary
clear privilege separation
better protocol discipline
safer path to pyJoules/JouleHunter integration
```

Negative:

```text
first commit does not show real energy measurement
requires discipline to avoid overbuilding simulation
```

## Follow-up

After the deterministic daemon proves the boundary, implement `rapl-wattchd` as the first privileged real backend.
