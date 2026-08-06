# Sandbox Runtime

## Objective

Repogents must isolate repository-agent commands without requesting elevated
privileges or changing host security policy. Sandbox selection is a runtime
capability decision and does not constrain the work graph, repository domain,
or agent procedure.

## Runtime Contract

Repogents uses Landlock for local repository-agent execution. Service startup
and direct runtime use execute `true` through the complete Landlock command
wrapper before creating an agent. If that preflight fails, execution fails
closed; Repogents never runs the command without the sandbox.

The environment must:

- clear the controller process environment before running agent commands;
- expose only the fixed command `PATH`;
- permit repository workspace reads and writes;
- permit private runtime temporary storage;
- permit reads and execution from required operating-system runtime paths;
- deny filesystem access outside those roots; and
- terminate the complete command process group on timeout.

The local sandbox does not isolate networking. Work requiring a stronger
operating-system, process, dependency, or network boundary belongs in a
separately implemented container runtime rather than an implicit local
fallback.

## Implementation Plan

1. Provide a mini-swe-agent-compatible Landlock environment in Repogents.
2. Verify Landlock at service startup and before direct runtime use.
3. Keep application configuration free of host privilege and sandbox-selector
   settings.
4. Test fail-closed preflight, environment clearing, workspace writes,
   outside-workspace denial, timeout cleanup, and service wiring.
5. Run focused tests, a real host production-path isolation probe, and the
   complete test suite.
