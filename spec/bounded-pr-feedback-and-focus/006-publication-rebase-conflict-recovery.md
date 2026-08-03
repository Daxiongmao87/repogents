# Publication Rebase Conflict Recovery

## Relationship to Prior Contract

This item changes only the validation-candidate fetch/rebase behavior observed by `003-single-issue-pr-silence-focus.md`. It does not change the guarded direct-target publication contract in `004-direct-target-publication-after-silence.md`.

## Scope

When validation candidate preparation encounters a Git rebase conflict after the configured target branch advances, Repogents must restore the issue workspace to a usable non-rebasing state, preserve the issue work, record the conflict as a failed validation for the current pass, and start a validation-failure pass in `SPECIFYING` rather than repeatedly remaining in `VALIDATING` with an unresolved Git operation.

## Acceptance Criteria

- [x] A validation-candidate rebase conflict restores the issue workspace to a usable non-rebasing state while preserving the committed issue work.
- [x] The conflict is recorded as a failed validation for the current pass, and the run starts one validation-failure pass in `SPECIFYING` instead of retrying the conflicted workspace in `VALIDATING`.

## Verification

- [x] A real-repository GitHub adapter regression proves a conflicting target update raises the bounded conflict outcome and leaves the issue workspace clean, non-rebasing, and on its preserved issue commit.
- [x] An Application regression proves the bounded conflict outcome records a failed validation, creates one validation-failure pass, and transitions the run to `SPECIFYING` without calling Validate.
- [ ] The deployed issue 28 run resumes from its preserved pass, converts the existing rebase conflict into a validation-failure pass, and continues through the end-to-end workflow.
