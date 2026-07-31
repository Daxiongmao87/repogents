# Repogents

Repogents is a local, restart-safe repository agent for one user on one Linux host. Add GitHub repositories, delegate an issue with the `agent:ready` label, and Repogents will inspect the repository, work in an isolated checkout, run the repository's validation commands, and open an unmerged pull request. It then monitors that pull request until GitHub reports it merged or closed, resolving new feedback on the same branch for as long as it remains open.

Repogents is intentionally an orchestrator, not a general-purpose agent shell. Repository commands run through a constrained Bubblewrap sandbox; GitHub and model credentials stay in the controller process.

## What it does

1. Onboards one or more GitHub repositories and records repository structure, manifests, validation commands, sandbox constraints, a repository-specific atomic agent team, and a model-designed workflow graph without generating dependency-provisioning commands.
2. Stores versioned repository environments, teams, and workflow templates for reuse across issues and restarts.
3. Watches onboarded repositories for GitHub events that apply `agent:ready` to an issue.
4. Creates one durable run, isolated checkout, and immutable issue graph for that activation.
5. Requires the coordinating member to persist an issue-bound atomic specification and an independent verifier to approve that exact revision before assignment or source mutation.
6. Advances dependency-ready agent and registered deterministic nodes, safely overlaps independent work, durably preserves node attempts and outputs, then commits and validates the requested change.
7. Binds exact-SHA independent acceptance to the approved specification, then reviews the complete committed diff for scope and secrets before publishing one deterministic, unmerged pull request.
8. Ingests reviews, inline review comments, and pull-request comments; reconciles changed requirements into a newly approved specification revision before source work, and updates the same pull request when source changes are required.
9. Continues polling every open application-owned pull request; a merge ends the run, while closure without merge starts one fresh run when the linked issue remains open.

Repogents never merges or closes pull requests.

## Requirements

- Linux
- Python 3.10 or newer
- Git
- [Bubblewrap](https://github.com/containers/bubblewrap) available as `bwrap`
- A GitHub identity authorized for every repository you onboard
- A model supported by mini-SWE-agent's LiteLLM adapter and the matching provider credential
- Repository-specific runtimes or licensed dependencies required by the repositories you choose

On Ubuntu, the baseline host packages are typically:

```bash
sudo apt install bubblewrap git python3 python3-venv
```

The GitHub CLI is optional. Repogents uses `GITHUB_TOKEN` or `GH_TOKEN` when present and otherwise attempts to read the active `gh auth` token.

## Install

```bash
git clone https://github.com/Daxiongmao87/repogents.git
cd repogents
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
```

## Configure

Model-backed work requires an explicit model selector, but the server can start before one is configured. Repogents never discovers model settings from another agent tool or from a user-level mini-SWE configuration. You can provide bootstrap values through the environment:

```bash
export REPOGENTS_MODEL="openai/<model-id>"
export OPENAI_API_KEY="<key>"
```

Use the credential variable required by the selected provider. Do not put credentials in this repository or in repository-specific input JSON.

Authenticate GitHub with either:

```bash
gh auth login
```

or a controller environment variable:

```bash
export GITHUB_TOKEN="<token>"
```

The token must be authorized to read the selected repositories and issues, push branches, create pull requests, read pull-request feedback, and post responses. For private repositories, it must also have access to the repository itself.

### Core configuration

| CLI option | Environment variable | Default | Purpose |
| --- | --- | --- | --- |
| `--data-dir PATH` | `REPOGENTS_DATA_DIR` | `~/.local/share/repogents` | Durable database, repository environments, run evidence, logs, and model state |
| `--model SELECTOR` | `REPOGENTS_MODEL` | none | Optional bootstrap LiteLLM model selector; required before model-backed work |
| `--model-base-url URL` | `REPOGENTS_MODEL_BASE_URL` | provider default | Optional bootstrap OpenAI-compatible endpoint |

Command-line options take precedence over environment defaults. Browser-saved provider settings then become authoritative across restarts. Global options must appear before the subcommand.

## Start Repogents

```bash
repogents serve
```

The default interface is available at <http://127.0.0.1:8765>. The scheduler polls every 10 seconds.

```text
repogents [--data-dir PATH] [--model SELECTOR] [--model-base-url URL] serve \
  [--host HOST] [--port PORT] [--poll-interval SECONDS]
```

The web interface has no account system and is intended for a single local user. Keep it bound to loopback. On a remote or headless host, use a trusted tunnel instead of exposing the port directly:

```bash
ssh -L 8765:127.0.0.1:8765 user@host
```

Then open <http://127.0.0.1:8765> on your local machine. Do not bind Repogents to a public interface without a separately authenticated, trusted access layer.

### Configure the model provider in the browser

Open **Model provider** at the top of the dashboard:

1. Set **API endpoint** only when using a custom OpenAI-compatible endpoint; leave it blank for the provider default.
2. Enter the **API key**. The key is write-only: later page loads show only whether it is configured.
3. Set the required **Default model** LiteLLM selector, such as `openai/<model-id>`.
4. Optionally set separate **Lead**, **Implementer**, and **Verifier** model selectors. Blank role fields inherit the default.
5. Select **Save model configuration**.

Endpoint and key changes apply to subsequent inference without restarting Repogents. New model selectors are stored by future repository onboarding; select **Re-onboard** for an existing repository when its immutable stored team should use the new selectors.

Non-secret settings are stored under the durable data directory. The API key is stored separately in a mode-`0600` file and is never returned by the HTTP API. A blank key field preserves the current key; **Remove stored API key** explicitly deletes a dashboard-saved key. A provider credential inherited from the daemon environment remains active until that environment is changed.


### Trusted-LAN user daemon

`deploy/systemd/repogents.service` is an explicit opt-in user service for a checkout at `~/projects/repogents`. It uses the checkout's `.venv`, restarts after failure, and reads host-specific values from `~/.config/repogents/daemon.env`.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
install -Dm644 deploy/systemd/repogents.service \
  ~/.config/systemd/user/repogents.service
install -d -m700 ~/.config/repogents
touch ~/.config/repogents/daemon.env
chmod 600 ~/.config/repogents/daemon.env
```

Configure one concrete LAN address rather than `0.0.0.0` so the browser authority continues to match Repogents' exact `Host` and `Origin` checks:

```text
REPOGENTS_DATA_DIR=/home/you/.local/share/repogents
REPOGENTS_MODEL=openai/<model-id>
REPOGENTS_LAN_HOST=192.168.1.20
REPOGENTS_LAN_PORT=8766
```

Provider credentials may instead be added to that mode-`0600` environment file as bootstrap/fallback values; never commit them. Then enable the daemon:

```bash
systemctl --user daemon-reload
systemctl --user enable --now repogents.service
```

To keep this checkout synchronized with `origin/main`, install and enable the
updater timer:

```bash
install -Dm644 deploy/systemd/repogents-updater.service \
  ~/.config/systemd/user/repogents-updater.service
install -Dm644 deploy/systemd/repogents-updater.timer \
  ~/.config/systemd/user/repogents-updater.timer
systemctl --user daemon-reload
systemctl --user enable --now repogents-updater.timer
```

The timer checks about once per minute. A clean local `main` is fast-forwarded,
then `repogents.service` is restarted and the restarted commit is recorded.
Tracked changes, another checked-out branch, or a non-fast-forward remote are
refused without changing the checkout or restarting Repogents. Untracked
application state does not block an update.

Every device that can reach the selected address can open this unauthenticated single-user interface. Use this mode only on a trusted LAN, never forward the port to the internet, and retain the loopback/tunnel setup on untrusted networks. The LAN service uses plain HTTP, so enter API keys only from a trusted network path. Dashboard state refreshes every 10 seconds; selected-repository activity is pushed immediately over a reconnecting Server-Sent Events stream.

## Onboard a repository

In the browser:

1. Enter a GitHub URL or `owner/repository`.
2. Leave **Repository inputs** as `{}` unless the repository needs an explicit host path, service, secret reference, or command override that cannot be inferred.
3. Select **Onboard**.
4. Wait for the repository state to become `ready`. A blocking reason remains visible when Repogents needs an irreducible input or encounters a setup failure.

You can also onboard from the command line:

```bash
repogents onboard owner/repository
```

Use `--inputs-json` only for explicit repository-specific requirements:

```bash
repogents onboard owner/repository --inputs-json '{
  "allowed_services": ["packages.example.com:443"],
  "validation_commands": [["python", "-m", "unittest"]]
}'
```

Supported input keys are:

- `allowed_host_paths`: explicit host mounts with `path`, optional sandbox `target`, and `mode` set to `read-only` or `writable`;
- `allowed_services`: exact `host:port` destinations available through the restricted proxy;
- `secret_bindings`: command-scoped secret references;
- `validation_commands`: an explicit list of command argument arrays.

These values are privileged configuration. Add only what a repository actually requires. Onboarding does not install repository dependencies or toolchains; agents retrieve missing dependencies inside bounded issue or acceptance actions. Re-onboarding creates new immutable sandbox and team versions; existing runs retain the versions with which they started.

### Command-scoped repository secrets

Never place a secret value in `--inputs-json`. Store it in the controller environment and refer to it by name. For example, `secret://package-token` resolves from `REPOGENTS_SECRET_PACKAGE_TOKEN` and is made available only to the commands listed in that binding:

```json
{
  "secret_bindings": [
    {
      "name": "PACKAGE_TOKEN",
      "reference": "secret://package-token",
      "commands": [["python", "provision.py"]]
    }
  ]
}
```

Secret values are command-scoped, redacted before durable output is stored, excluded from model context, and checked against committed changes before publication.

## Delegate issues

Repositories remain label-gated by default: apply `agent:ready` to delegate an issue as described below. For a ready, enabled repository, the dashboard also provides **Enable autonomous mode**, a durable per-repository opt-in that discovers all eligible open issues without requiring the label, including issues already open when the mode is enabled and issues opened later. Closed issues and pull-request records are excluded, and repeated polling or restarts do not duplicate an activation or an existing nonterminal run.

Autonomous mode controls issue selection only. It does not bypass onboarding readiness, resume a paused repository, revive a removed repository, or change the repository's stored sandbox, team, and intended base lineage. The selected value persists across application restarts and can be turned off with **Disable autonomous mode** to restore label-only discovery.

1. Create or select an issue in an onboarded repository.
2. Apply the `agent:ready` label, unless that repository has autonomous mode enabled in the dashboard. Autonomous mode also discovers eligible issues that were already open when it was enabled.
3. Leave Repogents running, or run a single orchestration cycle with `repogents tick`.e with `repogents tick`.

The activating label event or autonomous issue discovery has a stable identity. Repeated polling and application restarts reuse the same run instead of creating duplicates. Repogents snapshots the intended base branch and commit, creates an isolated checkout, and exposes repository and issue evidence to the coordinating member. The coordinating member persists the complete atomic issue specification; an independently modeled verifier must approve that exact revision before any implementation assignment or checkout mutation.

Each run graph is bound to the stored issue, team, sandbox, exact base SHA, and active specification revision. The coordinating member can assess completed node evidence and propose a bounded new graph generation; the controller validates the revision, preserves prior generations, and reuses only exact-identity outputs. Repository and run views expose the stored specification history, review provenance, acceptance mapping, topology, live states, attempts, generation deltas, reuse decisions, and assessments.

When the exact commit passes every discovered validation command, the approved specification's independent acceptance, scope review, and secret scan, Repogents pushes a deterministic `agent/issue-<issue-number>-<run-id>` branch and opens one pull request. The pull request remains unmerged for you to review.

## Feedback monitoring

Keep Repogents running while a pull request is open. It polls submitted reviews, inline comments, general pull-request comments, and pull-request status independently of repository-local agent work. New or edited feedback is persisted before evaluation. Valid changes are implemented, revalidated, and pushed to the same branch; questions and rejected requests receive a response without inventing a source change.

After all observed feedback is resolved, the same run remains in `waiting_for_feedback`. Elapsed time does not complete the run or stop polling. A merge moves the run to `closed`. Closing the pull request without merge closes that attempt and creates one fresh run from the repository's current stored sandbox and team when the linked issue remains open.

## Other commands

Run one scheduler cycle:

```bash
repogents tick
```

Print current durable inventory and run state as JSON:

```bash
repogents state
```

Both commands use the same global `--data-dir`, `--model`, and `--model-base-url` configuration as `serve`.

## Durable state

The default data directory is `~/.local/share/repogents`. It contains the authoritative SQLite database plus repository environments, isolated run directories, validation logs, and bounded model trajectories. The directory is created with owner-only permissions.

Do not place the data directory inside this Git checkout. Stop Repogents before making a filesystem-level backup, and preserve the complete directory rather than copying only the SQLite file.

## Security model

- Repository commands execute in Bubblewrap with explicit mounts, isolated namespaces, and restricted network egress.
- Each run has a separate writable checkout and output paths.
- GitHub and model credentials are controller-only and are not exposed to repository commands.
- Repository secret bindings are resolved from controller environment variables only for their authorized commands.
- Logs and model context are redacted; committed diffs are scanned for credential patterns before publication.
- The local interface requires same-origin mutation requests but has no user authentication. Loopback or a trusted authenticated tunnel is required.

The sandbox protects unrelated host resources from accidental or defective repository commands. It is not a security boundary for deliberately malicious repositories selected and authorized by the user.

## MVP limits

Repogents currently supports:

- one user;
- one Linux host;
- local SQLite and filesystem storage;
- one stored environment and team lineage per repository;
- unmerged pull-request delivery.

It does not provide multiple users or tenants, distributed workers, high availability, automatic merging, container orchestration, CPU or memory quotas, remote artifact storage, or protection against deliberately malicious repositories.

## Development

Install the project in a virtual environment, then run the complete suite:

```bash
python -m unittest discover -s tests -v
```

The behavioral contract and completed acceptance evidence live under `spec/`; the product-level MVP contract is in [`MVP.md`](MVP.md).
