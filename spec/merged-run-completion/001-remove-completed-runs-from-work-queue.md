# Remove Completed Runs from the Work Queue

Changes the active-run presentation and ordering established by `spec/repository-agent-mvp/010-local-interface.md` and `spec/issue-priority-queue/001-draggable-issue-priority-and-focus.md`, the reason presentation established by `spec/concise-issue-error-display/001-bound-error-summary-on-issue-cards.md`, and the pull-request body contract established by `spec/repository-agent-mvp/007-pull-request-publication.md`.

## Contract

- The **Issues and Runs** priority queue contains only nonterminal runs. `closed` and `canceled` runs remain durable in SQLite and available to internal reconciliation and run-specific evidence APIs, but they are not draggable work and do not appear in the active queue.
- Queue positions shown to the user are contiguous positions in the visible active order, independent of historical terminal-run priority values.
- Repository **Current run** state derives from the first visible nonterminal run; when none exists it reports no current run. Repository activity timestamps may still include terminal transitions.
- A run reason is styled as an error only when the visible run is `blocked`. Informational reasons on progressing or monitoring states use neutral presentation.
- A generated pull-request body contains the same-repository GitHub closing keyword `Closes #<issue-number>`. Repogents still never merges or closes the pull request itself; when the user merges it into the default branch, GitHub closes the linked issue.
- Priority and force mutations operate only on nonterminal runs and cannot make a terminal run actionable. Terminal evidence is never deleted.
- The already merged Martite pull request 9 is reconciled by closing its resolved GitHub issue 7 after the source change and deployed queue behavior are verified. No comment, label mutation, pull-request mutation, or new run is created.

## Acceptance Criteria

- [x] A merged or canceled terminal run disappears from the active priority queue while its complete durable database record remains intact.
- [x] Remaining queue cards show contiguous priority positions and reordering accepts exactly the visible nonterminal runs.
- [x] Repository status does not present a terminal run as the current run.
- [x] Blocked reasons remain red while nonblocked informational reasons are not rendered as errors.
- [x] Newly generated pull-request bodies link their issue with `Closes #<issue-number>` while remaining intentionally unmerged by Repogents.
- [x] The deployed dashboard no longer shows Martite issue 7 in the active queue, and GitHub issue 7 is closed after its merged pull request is verified.

## Verification

- [x] `UNIT` — seed active, blocked, closed, and canceled runs; verify state projection, current-run status, visible ordering, and terminal-preserving priority mutations.
- [x] `CLIENT` — render multiple active runs with sparse durable priorities; verify contiguous labels, blocked-only red styling, and absence of terminal cards.
- [x] `UNIT` — publish a fixture pull request and verify its body contains the exact closing keyword for the linked issue.
- [x] `REGRESSION` — run focused application, interface, and publication tests plus the complete project suite.
- [x] `DEPLOYMENT` — restart `repogents.service`, preserve durable run/focus state, and confirm the read-only live dashboard excludes terminal Martite issue 7 without changing queue order.
- [x] `GITHUB` — verify Martite pull request 9 is merged, close issue 7 without a comment or label change, and confirm the issue is closed.
