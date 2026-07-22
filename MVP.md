# MVP Requirements

## MVP Definition

This MVP is the smallest locally operated product that proves the complete repository-agent loop for one user:

1. onboard and persist multiple repositories;
2. create and reuse a repository-specific sandbox and agent team;
3. activate work from a GitHub issue labeled `agent:ready`;
4. implement and validate the requested change in an isolated checkout;
5. publish an unmerged pull request containing the tested revision;
6. resolve later pull-request feedback autonomously; and
7. notify the user after 30 continuous feedback-free minutes.

The MVP must make that complete path usable and restart-safe. It need not support multiple users, multiple hosts, distributed execution, high availability, or automatic merging.

## Product Outcome

One user can maintain a repository inventory, delegate a GitHub issue with `agent:ready`, have the repository's stored agent team implement and test it in the repository's stored sandbox, receive an unmerged pull request, have pull-request feedback resolved autonomously, and receive a notification after 30 quiet minutes.

## Operating Scope

- One user.
- One Linux PC.
- One locally running application.
- One configured GitHub identity.
- One configured agent/model runtime.
- Multiple inventoried repositories.
- Trusted repositories explicitly selected by the user.
- No automatic merging or closing of pull requests.

The sandbox protects unrelated host resources from accidental or defective repository commands. Resistance to deliberately malicious repository code is not an MVP guarantee.

## 1. Local Persistence

The application uses local durable storage consisting of:

- one SQLite database for identities, configuration, state transitions, external-object identifiers, deadlines, and notifications; and
- application-owned filesystem directories for sandbox environments, isolated checkouts, caches, logs, and generated run artifacts.

The database is the source of truth for repository, run, publication, feedback, and notification state. State required for restart recovery must be committed before the application performs the next external side effect.

The application stores at least:

- repositories and onboarding state;
- sandbox and team versions;
- issues and activating label-event identities;
- issue runs and their current states;
- run-specific base branches and base commit SHAs;
- issue branch names, pull-request identities, and tested commit SHAs;
- validation commands and results;
- feedback versions and processing state;
- quiet-period generations and deadlines; and
- durable user notifications.

Database constraints must prevent:

- more than one run for the same activating label event;
- more than one nonterminal run for the same repository issue;
- duplicate storage or processing of the same feedback version;
- more than one pull-request association for the same run; and
- more than one notification for the same quiet-period generation.

The inventory, sandbox state, team composition, active runs, feedback state, quiet-period state, and notifications persist across application restarts.

## 2. Repository Onboarding and Inventory

The local interface allows the user to add GitHub repositories.

### 2.1 Repository inputs

Adding a repository accepts its GitHub identity and any repository-specific inputs that cannot be derived safely from source inspection, including, when applicable:

- explicitly allowed host paths and whether each is read-only or writable;
- repository provisioning commands or environment definitions;
- licensed or proprietary dependency locations;
- fixtures or dataset locations;
- sandbox-scoped secret references;
- permitted external-service hostnames; and
- user overrides for validation commands when repository discovery is incomplete.

The application stores secret references and required access policy, not raw application credentials in the repository record.

### 2.2 Onboarding execution

When a repository is added:

1. The application resolves and stores its stable GitHub repository identity and current default branch.
2. It creates the repository's persistent sandbox filesystem state and launch configuration.
3. One lead agent inspects:
   - repository instructions;
   - source structure;
   - manifests and lockfiles;
   - build configuration;
   - test configuration; and
   - repository-defined validation commands.
4. The sandbox is provisioned with the required environment using discovered information and supplied repository inputs.
5. The lead formulates the repository-specific agent team from repository evidence.
6. The application stores versioned sandbox and team records with the repository.
7. The repository enters `ready` only after its stored environment and team can be loaded successfully.

If a required license, credential reference, host path, dataset, external service, or provisioning input cannot be derived, onboarding enters `needs_input` and identifies the missing input. If provisioning or inspection fails for another reason, onboarding enters `blocked` and records the failure.

### 2.3 Inventory contents

Each inventory entry contains:

- stable GitHub repository identity;
- repository URL and display name;
- stored default branch;
- local sandbox inputs;
- stored sandbox filesystem and launch-configuration version;
- stored agent-team version;
- onboarding state and any blocking reason; and
- links to active issues and runs.

The application displays every inventoried repository. Repositories in `needs_input` or `blocked` remain visible.

The stored sandbox and team are not reconstructed for each issue. They remain authoritative until explicit repository re-onboarding creates a new version. Existing issue runs continue to reference the sandbox and team versions they loaded.

## 3. Stored Repository Sandbox

### 3.1 Persistent environment

Each repository has persistent sandbox filesystem state and Bubblewrap launch configuration containing or referencing its required:

- development tools;
- baseline project dependencies;
- licensed or proprietary dependencies;
- environment-specific configuration;
- fixtures and datasets;
- generated files;
- caches; and
- sandbox-scoped sensitive-value bindings.

Bubblewrap processes are instantiated from this stored state for onboarding, issue work, validation, and feedback handling. The stored repository environment is reused for later issues and is not reprovisioned for every issue.

### 3.2 Storage layers

The sandbox separates three kinds of state:

1. **Persistent repository state** — toolchains, baseline project dependencies, proprietary dependencies, fixtures, datasets, and stored environment configuration.
2. **Persistent shared caches** — package downloads, language package stores, compiler caches, and other concurrency-safe reusable caches.
3. **Issue-run state** — the isolated checkout, writable dependency deltas, build output, temporary diagnostic tools, agent state, coordination data, logs, and temporary files.

Each issue receives its own isolated checkout and run storage. It may reuse persistent repository dependencies and caches without reinstalling unchanged dependencies.

Issue-specific dependency installation is incremental:

- missing or changed dependencies are installed into the issue-run writable layer;
- unchanged repository dependencies and cached artifacts are reused;
- temporary diagnostic dependencies may be discarded with the run;
- a dependency introduced by an unmerged branch is not promoted into the repository baseline merely because that branch was tested; and
- a later run based on a default branch containing that dependency may reconcile the stored environment incrementally.

Parallel issue runs must not share writable checkout, build-output, dependency-delta, log, or agent-state directories. Shared caches must use the package manager's or cache implementation's concurrency controls.

### 3.3 Filesystem and process isolation

Agents' repository access and all repository-controlled commands execute through the repository sandbox.

The orchestration controller, GitHub client, and model client remain outside the repository command environment. Their credentials are never mounted into the sandbox or inherited by repository-controlled processes.

For each sandboxed process, Bubblewrap must ensure:

- only explicitly configured host paths and application-owned sandbox paths are mounted;
- mount permissions match the stored repository policy;
- unrelated host files and credentials are inaccessible;
- unrelated host processes and IPC endpoints are inaccessible;
- host service sockets are not mounted;
- repository and run artifacts are stored in application-owned paths rather than the repository branch; and
- the process belongs to a supervised run process tree that can be terminated completely.

A representative application-owned layout is:

```text
application-data/
  repositories/<repository-id>/
    sandbox/
    team/
    caches/
    runs/<run-id>/
      checkout/
      agent-state/
      logs/
      temp/
      validation/
```

The isolated checkout is the only version-controlled working tree. Plans, coordination data, logs, caches, secret bindings, environment configuration, and sandbox artifacts live in sibling application-owned directories.

### 3.4 Network isolation

Sandboxed processes have no unrestricted route to the host network.

Required repository-command traffic passes through an application-managed, repository-specific egress proxy or equivalent restricted network path. The network policy must:

- allow only external services stored in the repository's sandbox inputs;
- resolve allowed hostnames through the controlled path;
- reject loopback, private-LAN, link-local, multicast, and metadata-service destinations after address resolution;
- reject access to host services; and
- record connection metadata without recording credentials or sensitive payloads.

Application-owned GitHub and model operations occur outside the repository command sandbox. Package-manager and other repository-command traffic uses the restricted egress path. If a required protocol cannot use that path, onboarding enters `needs_input` or `blocked` rather than granting unrestricted network access.

### 3.5 Sensitive values

The application must prevent its GitHub and model credentials from entering repository command environments, agent prompts, repository commits, or pull requests.

Sandbox-scoped sensitive values must be handled as follows:

- only bindings explicitly configured for the repository are available;
- a value is exposed only to the command that requires it, rather than inherited by every sandbox process;
- raw values are excluded from agent prompts and durable coordination state;
- known values are redacted from captured standard output and standard error before persistence or model submission;
- the committed diff is scanned for known sensitive values and credential patterns before publication; and
- publication is blocked if a potential secret is detected until the lead removes it or the user explicitly corrects the configuration.

These controls prevent accidental leakage within the trusted-repository threat model. They do not claim to prevent deliberate transformation or exfiltration by actively malicious repository code that is intentionally granted a secret.

### 3.6 Process lifetime and resources

No artificial CPU or memory quotas are imposed.

Commands may have wall-clock timeouts so a stuck command cannot run forever. Canceling a run terminates its supervised process tree, including descendants, without deleting the stored repository sandbox or persistent caches.

## 4. Stored Repository Agent Team

Every repository has one versioned, stored agent-team composition based on repository evidence.

The team always contains one lead responsible for the final result. It may also contain repository-appropriate:

- scout agents;
- implementation agents; and
- verification agents.

For each stored member, the application records:

- a stable member identity within the team version;
- role;
- repository-specific responsibilities;
- permitted tools;
- configured model/runtime selection; and
- repository-specific instructions.

The team composition:

- is formulated during repository onboarding;
- is based on the repository rather than an individual issue;
- persists across issues and application restarts;
- is loaded by version without reconstruction for each issue;
- is not derived from a mandatory permanent global team composition; and
- changes only through explicit repository re-onboarding.

Issue-specific assignment may vary:

- small issues may engage only the stored lead;
- larger issues may engage additional stored members;
- the lead records which stored members it assigns and why;
- agents work sequentially unless the assigned work is genuinely independent; and
- the lead integrates all work and owns the final implementation and validation decision.

## 5. Issue Activation and Run Lifecycle

### 5.1 Activation identity

The application polls every ready inventoried repository for GitHub issue events that apply `agent:ready`.

Each activating label event is identified by its stable GitHub event identity. When a previously unprocessed activation event is observed:

1. The application creates one durable run record in a database transaction.
2. The run references the repository's existing sandbox and team versions.
3. The run snapshots the repository's stored default branch as its intended base branch.
4. The application fetches and stores the base commit SHA used to create the checkout.
5. It creates isolated checkout and run-storage directories.
6. It stores the issue and current discussion for the lead.
7. It queues the stored lead to begin work.

The activating event identity is unique. Repeated polling and application restarts cannot create another run for the same event.

A repository issue may have at most one nonterminal run. Removing `agent:ready` does not implicitly cancel an existing run. A new label event may create a new run only after the previous run becomes terminal.

After restart, the application loads and reconciles the existing nonterminal run instead of creating another one.

### 5.2 Run states

The application exposes these issue-run states:

- `queued`;
- `implementing`;
- `validating`;
- `publishing`;
- `waiting_for_feedback`;
- `resolving_feedback`;
- `quiet_period`;
- `notified`;
- `blocked`;
- `canceled`; and
- `closed`.

`blocked`, `canceled`, and `closed` record a reason. `canceled` and `closed` are terminal. A blocked run performs no autonomous work and may be canceled; recoverable internal failures do not enter `blocked`.

A run may be `waiting_for_feedback`, `quiet_period`, or `notified` without retaining an agent process. New feedback for an open pull request returns the same run to `resolving_feedback`.

### 5.3 Failure handling

Recoverable model, controller-tool, validation-infrastructure, orchestration, and reconcilable external-operation failures preserve the current durable run state and are retried automatically on a later scheduler cycle. A run enters `blocked` only when work cannot continue because of:

- contradictory or insufficient requirements;
- missing repository inputs;
- unavailable dependencies or required services;
- failed authorization;
- an unrecoverable sandbox or checkout error;
- validation that the lead cannot make pass without violating scope; or
- a publication or feedback error that cannot be reconciled automatically.

The interface displays the blocking reason and the last completed state. Restart and automatic reconciliation resume the same nonblocked run and reconcile existing local and GitHub state before repeating an external operation.

### 5.4 Cancellation

The issue-run view allows the user to cancel a nonterminal run.

Cancellation:

- terminates the run's supervised process tree;
- marks the run `canceled`;
- retains its logs and durable state;
- retains the repository's sandbox and caches; and
- leaves any existing GitHub branch or pull request untouched.

## 6. Autonomous Implementation and Validation

For each activated issue, the stored lead and any stored team members it assigns:

- inspect the issue and its discussion;
- inspect repository instructions and relevant current source;
- determine the requested behavior and scope;
- modify only the isolated issue checkout;
- reuse stored project dependencies and caches;
- install only missing issue-specific dependency deltas inside the sandbox;
- run repository-required tests and validation inside the sandbox; and
- revise the implementation until the requested behavior is implemented and required validation passes or the run must become blocked.

The lead remains responsible for deciding whether the implementation satisfies the issue and repository instructions.

Every validation result records:

- run identity;
- exact commit SHA;
- command;
- start and completion times;
- exit status; and
- external log location outside the repository branch.

A validation result applies only to its recorded commit SHA. Publication is allowed only when every required validation command has a passing result for the commit being published.

After feedback changes source, the lead reruns validation affected by that change. If feedback produces no source or repository-configuration change, source validation is not required unless the feedback evaluation identifies a reason to rerun it.

## 7. Pull-Request Publication

### 7.1 Publication identity

Each run receives one deterministic issue branch name, using the form:

```text
agent/issue-<issue-number>-<run-id>
```

Before publication, the application durably stores:

- issue branch name;
- intended base branch;
- base commit SHA;
- validated head commit SHA; and
- passing validation records for that head.

The application-owned GitHub client performs pushes and pull-request operations outside the repository command sandbox. GitHub credentials are not exposed to agents or repository commands.

### 7.2 Publication checks

Before pushing, the lead and application must:

1. compare the validated commit with the stored base SHA;
2. inspect the complete committed diff for issue scope;
3. confirm that plans, coordination state, logs, caches, credentials, licensed artifacts, and environment configuration are outside the commit;
4. scan committed changes for known secrets and credential patterns; and
5. confirm that all required validation passed for the exact head SHA.

After pushing, the application verifies that the remote branch head equals the validated SHA. The pull request targets the run's stored intended base branch.

The application never merges or closes the pull request.

### 7.3 Restart-safe reconciliation

Publication must reconcile local state against GitHub before creating or repeating an external operation.

After an automatic reattempt or restart:

- if the deterministic branch already exists at the validated SHA, it is not pushed again;
- if the branch exists at an unexpected SHA, publication becomes blocked rather than overwriting unknown work;
- if an agent-created pull request already exists for the run branch, the application stores and reuses it;
- if no pull request exists, the application creates one; and
- the application never creates a second pull request for the same run.

A run does not enter `waiting_for_feedback` until the application has stored the pull-request identity and confirmed its head SHA.

## 8. Pull-Request Feedback Resolution

For every open agent-created pull request, the application polls for:

- submitted reviews and review bodies;
- inline review comments; and
- general pull-request comments.

### 8.1 Feedback identity and ingestion

Each observed feedback version is identified by:

- feedback type;
- stable GitHub object identity; and
- GitHub update version or `updated_at` value.

The application persists feedback before assigning it. Repeated polling cannot process the same version twice. An edited feedback object is processed as a new version of the existing object.

Application-created responses are recorded by their returned GitHub object IDs. Those objects are outputs, not new feedback. A manually posted comment that was not created and recorded by the application remains feedback even when it uses the same configured GitHub identity.

If the application crashes while posting a response, it reconciles the pending outbound operation before ingesting new feedback or posting again. Reconciliation compares the target thread, author, body, and attempted publication time with current GitHub state.

### 8.2 Evaluation and action

New feedback is given to the stored repository lead and handled using the run's stored repository team and sandbox versions.

The lead evaluates feedback against:

- the original issue;
- issue discussion;
- repository instructions;
- current implementation;
- previous pull-request discussion; and
- pull-request scope.

The team:

- implements valid in-scope change requests;
- answers relevant questions;
- explains or declines incorrect requests;
- declines out-of-scope requests; and
- avoids unrelated changes.

After a source-changing revision:

1. affected validation is rerun against the new commit SHA;
2. the tested revision is committed;
3. the application pushes exactly that revision to the existing pull-request branch;
4. the remote head SHA is confirmed; and
5. any required response is posted and recorded.

Review handling does not reprovision the sandbox or reformulate the team.

Feedback is processed in durable order. After resolving the current pending set, the application immediately polls again before starting a quiet period so feedback arriving during implementation is not missed.

## 9. Quiet-Period Notification

Each quiet period is a durable generation associated with the pull request.

The first quiet period starts only after the initial tested pull request is published and its remote head is confirmed.

For every later feedback cycle:

1. New feedback cancels the current quiet-period deadline.
2. The application resolves all feedback through the current durable watermark.
3. It publishes every required tested revision and response.
4. It polls again for feedback that arrived during resolution.
5. If no feedback remains pending, it stores a new quiet-period generation with a deadline 30 minutes after the final required publication.

At the first successful poll at or after the deadline, the application checks GitHub again. If:

- no newer unprocessed feedback exists;
- no feedback remains pending or in progress; and
- the pull request remains open,

then the application creates exactly one notification for that quiet-period generation and marks the run `notified`.

If the application or GitHub is unavailable at the deadline, notification waits until the application can successfully verify the pull request. It does not infer quiet time while unable to poll.

If a user or other external actor closes or merges the pull request, the application marks the run `closed`, stops its quiet period, and does not send a quiet-period notification for that interrupted generation. The application itself never merges or closes the pull request.

Feedback arriving after a notification returns the same open run to `resolving_feedback` and may produce another quiet-period generation and notification.

## 10. Minimal Local Interface

The application provides one local interface that allows the user to:

- add repositories and provide required repository-specific sandbox inputs;
- view all inventoried repositories;
- view repository onboarding state and blocking reasons;
- explicitly re-run repository onboarding when the stored environment or team must be refreshed;
- view active issues and runs;
- view each issue's current state and blocking reason;
- cancel a blocked or active run as applicable;
- open issue links on GitHub;
- open pull-request links on GitHub; and
- receive and acknowledge quiet-period notifications.

A quiet-period notification is stored durably and displayed in the interface with:

- repository identity;
- issue identity and link;
- pull-request identity and link;
- notification time; and
- read or unread state.

A Linux desktop notification may also be emitted, but the persistent in-application notification is the acceptance source of truth.

No application accounts, organizations, tenant management, onboarding wizard, or administrative console are required.

## 11. MVP Acceptance

### 11.1 Acceptance fixtures

Acceptance uses:

- `https://github.com/Daxiongmao87/bazzeye` and `https://github.com/Daxiongmao87/foundry-portal` as initial public inventory and preliminary evidence;
- `https://github.com/Daxiongmao87/websesh/issues/1` as the required complete end-to-end fixture; and
- the GitHub identity, model runtime, and local application access required to execute the path.

Repository commands, sandbox dependencies and services, stored team composition, issue behavior, permitted changed files, and feedback response are inferred from repository, issue, discussion, and arriving GitHub evidence. They are not predefined fixture inputs. Preliminary inventory or component runs cannot substitute for the complete `websesh#1` path.

A comment manually posted through GitHub may use the configured GitHub identity. It is feedback when its GitHub object ID was not created and recorded by the application.

### 11.2 Inventory acceptance

- At least two repositories can be added.
- Each produces a stored sandbox version and stored team version.
- The two repositories remain separate and retain their own configuration.
- After restarting the application, both repositories and their stored configurations remain available.
- Loading either repository does not rerun onboarding unless explicitly requested.

### 11.3 Sandbox acceptance

On the actual Linux host, a representative sandboxed repository command demonstrates that:

- configured repository and run paths are accessible with the configured permissions;
- an unrelated host file is inaccessible;
- an unrelated host process or service is inaccessible;
- direct loopback, private-LAN, link-local, and metadata-service connections fail;
- an allowlisted required external service is reachable through the restricted path;
- application GitHub and model credentials are absent from the command environment; and
- canceling the run terminates a spawned child-process tree without deleting the repository sandbox.

A canary sandbox-scoped secret demonstrates that:

- it is made available only to its authorized command;
- its known raw value is redacted from persisted command output; and
- publication is blocked if the value is placed in a committed change.

### 11.4 End-to-end acceptance

One inventoried repository demonstrates the complete live path:

1. Its sandbox and repository-specific team are created and stored during onboarding.
2. Their stable version identities are recorded.
3. The fixture issue receives `agent:ready`.
4. Repeated polling and one application restart result in exactly one run for the activating label event.
5. The run references the previously stored sandbox and team versions without reconstruction.
6. The lead records its assignment decision and uses only appropriate members from the stored team, which may be only the lead.
7. The requested fixture change is implemented in the isolated checkout.
8. The exact repository validation commands pass for a recorded commit SHA.
9. An unmerged pull request containing only the permitted intended changes is published.
10. The remote pull-request head equals the commit SHA that passed validation.
11. Exactly one pull request is associated with the run, including after publication reconciliation.
12. Real review feedback arrives and is stored exactly once.
13. The stored team evaluates and resolves it according to its actual content.
14. Any required source revision is validated against its own commit SHA and pushed to the same pull request.
15. Any required response is posted once and is not reprocessed as feedback.
16. The application is restarted during the quiet period.
17. No further feedback arrives for 30 continuous verified minutes.
18. Exactly one persistent notification is created for that quiet-period generation.
19. The notification identifies and links the repository, issue, and pull request.
20. The application does not merge or close the pull request.

## 12. Explicit Non-Requirements

- Multiple users or tenants.
- Multiple worker hosts.
- Distributed execution.
- High availability.
- Temporal.
- PostgreSQL.
- Vault.
- Docker or runsc.
- S3 or remote artifact storage.
- Kubernetes or another orchestration platform.
- CPU or memory quotas.
- Protection against deliberately malicious repositories selected and authorized by the user.
- Automatic team reformulation for every issue.
- Automatic repository-environment reprovisioning for every issue.
- A general-purpose workflow, sandbox, or infrastructure platform.
- Automatic pull-request merging or closing.
