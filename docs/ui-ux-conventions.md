# Repogents UI/UX conventions

**Status:** Adopted design guidance  
**Scope:** The server-rendered, dependency-free web client in `repogents/http_api.py`  
**Audience:** Design, frontend, and UI-test contributors

## Purpose and product constraints

Repogents is an operational dashboard, not a marketing site. Its primary job is to let an operator answer, in order:

1. **What repositories are tracked?**
2. **What branch will each repository target?**
3. **What is the saved agent graph?**
4. **Which issue runs exist and what state is each in?**
5. **What branch, specifications, work items, and pull request belong to a run?**
6. **What can I do now?** Add a repository, remove one, or follow a pull request.

The existing client is one HTML document embedded in Python, using local CSS and JavaScript and a small JSON API. Recommendations therefore use semantic HTML, native controls, system fonts, CSS, and plain JavaScript. Core workflows must not require a CDN, web font, icon service, external component library, JavaScript framework, or third-party analytics. GitHub links are enhancements to the information already shown, not the only representation of state.

## Research basis

The conventions below synthesize stable guidance rather than visual trends:

- Nielsen Norman Group's usability heuristics: visibility of system status, match with real-world language, user control, consistency, error prevention, recognition rather than recall, and minimalist design ([10 Usability Heuristics](https://www.nngroup.com/articles/ten-usability-heuristics/)).
- WCAG 2.2: semantic relationships, keyboard access, focus visibility, contrast, status messages, reflow, target sizing, and consistent identification ([WCAG 2.2](https://www.w3.org/TR/WCAG22/)).
- WAI-ARIA Authoring Practices: prefer native HTML and use established keyboard behavior for disclosure and alert patterns ([APG](https://www.w3.org/WAI/ARIA/apg/)).
- GOV.UK Design System: explicit labels, actionable error summaries/messages, restrained status tags, and clear empty/error content ([Design System](https://design-system.service.gov.uk/)).
- GitHub Primer and IBM Carbon: consistent spacing/type/color tokens, data-dense layout, status labels, and progressive disclosure in developer and operational tools ([Primer](https://primer.style/), [Carbon](https://carbondesignsystem.com/)).

These sources agree that operational interfaces benefit more from predictable structure, legible state, and immediate feedback than from novelty. Repogents should borrow those principles, not reproduce any source's branding or components.

## Essential conventions

The following are requirements for downstream work, not optional polish.

### 1. Information hierarchy and page structure

Use one stable page hierarchy:

1. A compact product header with the `Repogents` name and one-sentence purpose.
2. A clearly titled repository-add region.
3. A tracked-repositories region with a visible count or explicit loading/empty state.
4. One article per repository.
5. Within each repository: identity and target branch first, repository action second, saved graph third, then issue runs.
6. Within each run: issue identity and lifecycle state first, branch and pull request second, specifications and work items third.

Use semantic landmarks and heading levels (`main`, `header`, `section`, `article`; a single `h1`, then ordered `h2`/`h3`). A heading must describe the content that follows; do not use size-only text as a substitute. Keep repository names and issue numbers visually stronger than metadata. Show labels such as “Target branch”, “Branch”, and “Pull request” rather than relying on position or punctuation.

**Why it fits:** operators repeatedly scan by repository, issue, and state. A predictable object hierarchy reduces recall and makes assistive-technology navigation useful.  
**Risk to avoid:** oversized branding or decorative hero content pushing current system state below the fold.

### 2. Navigation and progressive disclosure

Retain a single-page overview while the information volume is modest. Do not introduce global navigation with only one destination.

- Repository identity, target branch, run identity/state, and primary actions remain visible.
- Saved graph and current run summaries remain available without another page load.
- When density grows, native `<details>/<summary>` may collapse *secondary* run detail. The summary must still expose issue number/title, lifecycle state, branch presence, pull-request presence, and work progress.
- Default-expand active or failed runs. Completed historical runs may default collapsed.
- Never hide add/remove actions in hover-only menus. Do not use horizontal carousels for operational data.

**Why it fits:** disclosure controls density without removing context or creating a navigation burden. Native disclosure is keyboard operable and works without a library.  
**Tradeoff:** collapsing every run makes cross-run comparison harder; use it only after real density warrants it.

### 3. Action placement and control hierarchy

- The add form has one primary action, placed after its fields in reading and tab order.
- “Remove repository” is a secondary/destructive action in the repository header and includes the repository name in its accessible name (visible or via `aria-label`).
- Use native `<button>` for mutations and `<a>` for navigation. Never style a non-interactive element as a control.
- Destructive styling must differ by wording and shape/border treatment as well as color. A confirmation is appropriate because removal changes tracked state; name the affected repository and keep cancel as the easy/default choice.
- Disable only the submitted form/action while pending, preserve the label with a pending verb such as “Adding…”, and prevent duplicate submission.
- Minimum control height should be about 44 CSS pixels. Do not place compact destructive and primary targets so close that accidental activation is likely.

### 4. Status communication

Every status treatment has three layers:

1. **Text:** a human-readable label (for example, `In progress`, not only `RUNNING`).
2. **Non-color cue:** a consistent icon, glyph, or shape where useful; text always remains.
3. **Color:** restrained semantic emphasis, never the sole distinction.

Normalize machine states for display while retaining exact data when useful:

| Family | Examples | Suggested treatment |
|---|---|---|
| Neutral/queued | queued, not created, pending | Gray/neutral badge; clock or hollow marker |
| Active | running, specifying, validating, publishing, PR listening | Blue accent badge; activity marker; label such as “In progress” |
| Success | completed, published, passed | Green badge; check marker; “Completed”/“Passed” |
| Warning | waiting, blocked, needs input | Amber badge; warning marker; explicit next condition |
| Failure | failed, error, rejected | Red badge; error marker; concise failure wording |

Use a shared badge component/class for run and work-item states. Status labels use sentence case and should not look clickable. Node persistence (`Permanent`, `Persistent`, or other values) is metadata, visually subordinate to node classification; it must not be confused with run lifecycle status.

**Why it fits:** operators must recognize changing state quickly, including with color-vision differences or monochrome/high-contrast presentation.  
**Risk to avoid:** a rainbow of state colors with no shared semantics.

### 5. Feedback and system state

Feedback belongs near the workflow it describes and should also be announced when appropriate.

- **Initial loading:** show “Loading tracked repositories…” in the repository region. If loading becomes slow, use restrained skeleton blocks only when they preserve the expected layout; never show an indefinite blank panel.
- **Refresh:** retain the last valid repository content. A refresh must not replace it with a full loading state.
- **Refresh failure:** show a non-destructive inline warning above retained content (“Could not refresh. Showing information from …”) and provide a retry action or indicate automatic retry. Use `role="status"` for routine refresh notices; use `role="alert"` for a new error requiring attention.
- **Form pending:** associate pending state with the submit button and set `aria-busy="true"` on the form or relevant region.
- **Validation/API error:** render a concise message adjacent to the form in an alert region, associate field-specific errors through `aria-describedby` and `aria-invalid`, preserve entered values, and move focus only when needed to make an otherwise missed error discoverable.
- **Success:** update the repository list, show a short status message such as “Added owner/name”, and return the form to a useful state. Do not depend on a transient toast as the only evidence—the new repository itself is durable confirmation.
- **Removal:** keep the repository visible while pending; on success remove it and announce the named result. On failure restore the enabled action and retain content.
- **Empty:** explain both the state and the next useful action. Examples: “No repositories are tracked yet. Add a GitHub repository above.”, “No issue runs are queued.”, “No specifications yet.”, and “No work items yet.”

Reserve a stable amount of space for short form feedback where doing so prevents layout jump, but do not leave unexplained empty boxes. Announcements should be concise to avoid repeating the full page every three-second refresh.

### 6. Responsive and dense-data behavior

Design mobile-first and verify at content-driven widths rather than device names. The interface must reflow at 320 CSS pixels without horizontal page scrolling (WCAG reflow intent).

- Page gutters: 16px on narrow screens, increasing to 24–32px where space permits; cap the content width for readable scanning.
- Add form: one column on narrow screens; repository and branch fields may share a row at wider widths; submit stays visually attached to the form.
- Repository/run headers: wrap, then stack identity above actions. Do not truncate repository names or branch names without making the complete value available.
- Graph: use an ordered list or flex/grid sequence that wraps. Give every node an explicit sequence number or preserve DOM order with directional connectors that remain understandable after wrapping. On narrow screens, stack nodes vertically and orient connectors downward; never require horizontal graph panning for the saved sequence.
- Specifications/work items: stack sections on narrow screens and use two balanced columns only when each has adequate width.
- Long owner/name, branch, classification, and work titles: allow safe wrapping (`overflow-wrap: anywhere`) while preserving copyable text.
- Tables are not preferred for the current nested, variably sized content. If future comparison genuinely requires a table, keep real table semantics and transform presentation without changing reading order.

Suggested verification widths are 320px, 768px, and 1280px, plus zoom to 200% at desktop width. Breakpoints should be selected where content stops fitting, not to target brands of devices.

### 7. Accessible interaction

- All workflows must work with keyboard alone in logical DOM order; do not add positive `tabindex` values.
- Every input has a persistent visible `<label>`. Mark required fields in text and with the native `required` attribute; identify optional fields as optional.
- Show a strong `:focus-visible` indicator on links, buttons, inputs, summaries, and any custom control. It should remain distinguishable on all semantic backgrounds and not rely only on a subtle shadow.
- Preserve native focus behavior when content refreshes. Never replace a focused repository subtree during polling if unchanged; if a focused item is removed by the user's action, move focus to a logical heading/status or the next repository.
- External pull-request links identify both destination and behavior, e.g. “Pull request #17 (opens in a new tab)”. If opening a new tab, use `rel="noopener noreferrer"`; `noopener` is the security requirement.
- Use accessible names that include context for repeated controls (“Remove acme/widget”, not several buttons named only “Remove”).
- Meet WCAG AA contrast: at least 4.5:1 for normal text and 3:1 for large text and meaningful component boundaries/graphics. Check semantic badge combinations independently.
- Respect `prefers-reduced-motion`; no workflow requires animation. Respect forced-colors/high-contrast modes by retaining borders and system-recognized controls.
- Set touch/pointer targets to at least 24 by 24 CSS pixels per WCAG 2.2 AA, with approximately 44px as the preferred operational target.
- Avoid `aria-live` on the entire frequently replaced repository region. Use a small dedicated status node so polling does not repeatedly announce all content.

## Visual system direction

Downstream implementation should express these as centralized CSS custom properties and reusable classes. Exact values may be tuned after contrast and content testing, but one scale should be used consistently.

### Typography

- System stack only: `ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`; use `ui-monospace` for branch names and machine identifiers only.
- Base text: 16px with 1.5 line height. Supporting metadata may be 14px but not smaller.
- Use a restrained type scale, approximately 14 / 16 / 20 / 24 / 32px. Weight and spacing should establish hierarchy; avoid very large display type.
- Left-align operational text. Do not uppercase long labels or add tight letter spacing that harms readability.

### Spacing and layout

Use a 4px base with a practical scale such as 4, 8, 12, 16, 24, 32, and 48px. Adjacent items use 8–12px, component padding 16–24px, and section separation 24–32px. Prefer `gap` for layout over scattered one-off margins.

### Color

Define role-based tokens rather than component-specific hex values:

- canvas, surface, raised surface, border
- primary text, secondary text, muted text
- accent, accent hover
- focus ring
- success foreground/background/border
- warning foreground/background/border
- danger foreground/background/border
- neutral status foreground/background/border

The current dark presentation may remain, but contrast must be measured. A subtle canvas-to-surface distinction and borders are sufficient; gradients and glows must not reduce text contrast or become the main hierarchy. If light mode is added, it must be complete and tested rather than partially inheriting browser defaults.

### Surfaces, controls, and links

- Use low-elevation bordered surfaces for the add workflow and repository objects. Nested runs should rely on dividers or a slight surface shift rather than multiple strong shadows.
- Use one radius scale (for example 6px controls, 10–12px containers, pill only for badges).
- Primary buttons are filled; secondary buttons are quiet/outlined; destructive buttons use danger wording and semantic treatment; disabled controls remain legible and clearly inactive.
- Inputs and buttons share control height, border language, and focus treatment.
- Links are recognizable without hover (underline is preferred in body text). Pull-request links should carry the number as meaningful link text.

## Optional enhancements

These may follow only after essential workflows and states are complete:

- A repository/run count summary for faster orientation.
- Manual “Refresh” and visible “Last updated” metadata.
- Filter by run state when repository volume makes scanning difficult.
- Collapse completed historical runs while leaving active/failed runs open.
- Copy buttons for branch names, provided text remains selectable and the action has feedback.
- User-selectable light/dark theme using only local CSS and persisted preference.

Do not add search, charts, animation, icon packs, side navigation, or virtualized lists without demonstrated volume or user need.

## Rejected alternatives

| Alternative | Decision and rationale |
|---|---|
| Card-heavy dashboard with large metrics and charts | Reject for now. Repogents has object state and sequences, not meaningful aggregate KPIs; cards/charts would displace actionable detail. |
| Always-visible full detail for every historical run | Reject at scale. It harms scanability; retain summaries and progressively disclose secondary history. |
| Data table as the primary repository layout | Reject for current nested graph/run content and narrow-screen needs. A list of semantic articles reflows more naturally. |
| Color-only dots or borders for lifecycle state | Reject because state must survive color-vision differences and assistive technology. |
| Toast-only errors and success | Reject because transient, remote feedback is easy to miss and poorly associated with controls. |
| Hover-only actions or icon-only remove button | Reject for touch, keyboard discoverability, and destructive-action clarity. |
| Auto-refresh that clears content or steals focus | Reject because it interrupts inspection and turns transient network failure into loss of useful state. |
| External fonts, hosted CSS/JS, or remote icon sets | Reject because they add availability/privacy dependencies to a local operational tool. |
| Horizontal graph scroller as the only mobile strategy | Reject because sequence and actions become harder to discover; use a wrapping/vertical ordered sequence. |

## Implementation handoff checklist

The design-system and frontend work should:

1. Introduce centralized tokens for type, spacing, color, borders, radii, focus, controls, and status families.
2. Preserve every existing datum and action currently rendered by `repogents/http_api.py`.
3. Replace placeholder-only form identification with visible labels and required/optional guidance.
4. Build reusable patterns for repository header, graph sequence, run summary, status badge, empty state, and inline feedback.
5. Separate add-form errors from refresh errors and success/status announcements.
6. Retain valid state during refresh and avoid wholesale DOM replacement where it would disrupt focus or disclosure state.
7. Render machine statuses through a single label/status mapping with text and non-color cues.
8. Make graph order explicit after wrapping and stack graph/run detail at narrow widths.
9. Contextualize repeated destructive controls and safely mark external PR links.
10. Test keyboard operation, focus visibility, announcements, 320px reflow, long strings, 200% zoom, reduced motion, and AA contrast.

## Acceptance review prompts

Before considering the redesigned foundation complete, reviewers should be able to answer “yes” to all of these:

- Can a new user identify tracked repositories, target branches, active issues, and current states in one scan?
- Are graph order and node persistence understandable without relying on desktop-only geometry?
- Are branch, specification, work-item, and pull-request details still reachable?
- Does every loading, empty, pending, success, and failure condition say what happened and, where relevant, what to do next?
- Can all controls be found and operated by keyboard with a visible focus indicator?
- Are labels, names, headings, and changing statuses programmatically determinable?
- Does status remain understandable without color?
- At 320px and at 200% zoom, is there no horizontal page scroll and no hidden core action?
- If the network fails during refresh, does the last valid content remain usable?
- If every third-party host is unavailable, do all local core workflows still function?
