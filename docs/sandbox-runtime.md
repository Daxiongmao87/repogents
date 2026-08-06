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

System software remains read/execute-only from standard installation roots,
including `/usr`, `/opt`, and their resolved mount locations. Repository agents
retain write access only to their workspace and command temporary directory.
`HOME`, `TMPDIR`, and XDG state paths point into that private temporary directory
so tools can initialize without access to the controller user's home directory.
Read-only operating-system runtime metadata such as `/proc`, `/dev`, and `/sys`
remains available to installed tools.

The environment must:

- clear the controller process environment before running agent commands;
- expose only the fixed command `PATH`;
- permit repository workspace reads and writes;
- permit private runtime temporary storage;
- permit reads and execution from required operating-system runtime paths;
- deny filesystem access outside those roots; and
- deny IPv4 and IPv6 socket creation when agent internet access is disabled;
- preserve DNS, TLS, and outbound sockets when agent internet access is enabled; and
- terminate the complete command process group on timeout.

Agent internet access is an explicit opt-in controlled by
`REPOGENTS_AGENT_INTERNET_ACCESS` and defaults to disabled. Disabled mode uses
a seccomp rule layered with Landlock to reject `AF_INET` and `AF_INET6` socket
creation with `EPERM`; it does not simulate denial through broken DNS. Enabled
mode retains resolver paths and outbound sockets. Service startup probes the
configured behavior through the complete sandbox: disabled mode must observe
socket denial, while enabled mode must complete DNS resolution, TLS, and an
HTTP exchange. A failed probe stops startup.

## Implementation Plan

1. Provide a mini-swe-agent-compatible Landlock environment in Repogents.
2. Verify Landlock at service startup and before direct runtime use.
3. Keep internet access opt-in, disabled by default, and independent of graph
   semantics or agent procedure.
4. Test fail-closed preflight, environment clearing, workspace writes,
   outside-workspace denial, both network modes, timeout cleanup, and service
   wiring.
5. Run focused tests, a real host production-path isolation probe, and the
   complete test suite.
