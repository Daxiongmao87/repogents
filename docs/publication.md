# Publication Contract

Repogents prepares and maintains pull requests; merge authorization remains with
the repository owner unless automatic merge is explicitly enabled.

## Review Readiness

- A published or corrected pull request enters `PR_LISTENING`.
- The default review-silence interval is 3,600 seconds.
- New actionable feedback returns the run to agent work and a corrected head
  starts a new silence interval.
- When the interval expires, the default transition is `PENDING_MERGE`.
- `PENDING_MERGE` remains nonterminal. Repogents continues polling the pull
  request for new feedback, closure, or a user-performed merge.
- A user-performed merge transitions the run to `COMPLETED`; closing an
  unmerged pull request transitions it to `CLOSED`.

`REPOGENTS_AUTO_MERGE` defaults to `false`. Only an explicitly true value may
authorize the controller to publish the validated head to the target branch
after the silence interval.

## Pull-Request History

Each issue pull request exposes one squashed issue commit. The validation agent
returns a concise commit subject based on the completed work and validated diff.
The controller amends the prepared commit with that subject and updates the
issue branch with `--force-with-lease` against the remote head observed during
preparation. A changed remote head invalidates publication and requires a fresh
preparation and validation pass.

The pull-request body uses `Closes #<issue>` so GitHub closes the originating
issue only when the pull request is actually merged.
