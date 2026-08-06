# Issue Intake And Ordering

Repogents discovers every open GitHub issue in each tracked repository. By
default, only issues explicitly labeled `agent:ready` are authorized for a run.
Repository-wide autonomous issue intake is an explicit per-repository opt-in;
when enabled, every open issue is authorized without that label. Pull requests
returned by GitHub's issues API are excluded from issue intake.

Follow-up issues created from valid out-of-scope pull-request feedback are
created without `agent:ready` or any other execution label. Their provenance
marker remains the idempotency key for creation and acknowledgement.

## Ordering Agent

When a repository has more than one authorized open issue, the controller invokes an
out-of-graph issue-ordering agent. Like the out-of-graph Node Role Agent, this
agent is controller-scoped and never becomes a saved graph node.

The ordering agent receives the complete current authorized-issue snapshot,
repository identity, and durable run states. It returns every issue exactly
once in processing order. For each issue it must provide:

- a reason and concrete evidence for its position;
- every causal open-issue dependency;
- a reason and concrete evidence for each dependency.

The controller does not prescribe a priority or dependency taxonomy. It
validates issue identity, evidence completeness, dependency references,
uniqueness, and prerequisite-before-dependent ordering. Invalid plans fail
closed.

The accepted plan and the exact issue snapshot are persisted. The plan is
reused while that snapshot is unchanged and regenerated when an issue is
opened, closed, or its title, body, or URL changes. Active work is not
preempted; the accepted plan determines which queued issue gains focus next.
