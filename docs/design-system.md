# Repogents interface system

**Status:** Implemented foundation  
**Source:** `docs/ui-ux-conventions.md`  
**Runtime:** Embedded CSS in `repogents/http_api.py`; no remote assets or client dependencies

## Purpose

This system translates the adopted operational-dashboard conventions into a small, reusable set of design tokens and presentation patterns. It is intentionally restrained: repository and run information establishes hierarchy, while surfaces and semantic color support rather than compete with it.

Contributors should extend these variables and patterns instead of introducing component-specific colors, arbitrary spacing, or new control treatments. The embedded client in `repogents/http_api.py` owns the current repository-management, operational-status, and live-state behavior; this document defines the visual and interaction contract that those implementations follow.

## Token contract

### Typography

| Token | Value | Use |
|---|---|---|
| `--font-sans` | Local system UI stack | All interface text and controls |
| `--font-mono` | Local system monospace stack | Branches and machine identifiers only |
| `--text-xs` | 14px | Metadata, badges, hints; minimum supported text size |
| `--text-sm` | 16px | Body and control text |
| `--text-md` | 20px | Subsection headings |
| `--text-lg` | 24px | Section/object headings |
| `--text-xl` | 32px | Compact page title |
| `--leading-tight` / `--leading-normal` | 1.25 / 1.5 | Headings / body copy |

Use weight and spacing before increasing size. Long operational strings use wrapping rather than ellipsis. Do not use monospace for ordinary prose.

### Spacing and shape

The spacing scale is `--space-1/2/3/4/6/8/12`, corresponding to 4, 8, 12, 16, 24, 32, and 48px. Use `gap` for component internals and the shared tokens for padding and separation.

- `--radius-sm` is for controls.
- `--radius-md` is for surfaces.
- `--radius-pill` is reserved for non-interactive badges and compact graph nodes.
- `--control-height` is 44px, the preferred pointer target size.
- `--content-max` limits line length and scanning distance on wide displays.

### Surfaces and text

`--canvas`, `--surface`, and `--surface-raised` form the page depth sequence. `--border` (`#657693`) is the shared meaningful-boundary token for panels, repository cards, nested runs, dividers, and other information-bearing regions; it has at least 3:1 contrast against each of those three adjacent surface tokens (the minimum checked pair is 3.47:1 against `--surface-raised`). `--border-strong` (`#7486a4`) provides additional emphasis for controls and dashed or leading boundaries. These borders communicate grouping independently of shadows and subtle fill shifts. Use `--text`, `--text-secondary`, and `--text-muted` in decreasing order of importance.

The system is dark-only for now. Do not add a partial light theme. All colors are local values and do not depend on user-agent defaults except in forced-colors mode.

### Interaction and semantic colors

- `--accent`, `--accent-surface`, and `--accent-hover`: links and primary actions. Enabled primary controls use `--text` (`#eef3ff`) on `--accent-surface` (`#255ac7`) by default and `--accent-hover` (`#1d459b`) on hover; these normal-sized text pairs measure 5.62:1 and 7.95:1 respectively. The hover token is shared rather than Add-workflow-specific, remains visibly distinct from the default fill, and preserves stronger separation from the accent border.
- `--focus`: a high-visibility focus outline, separate from the accent.
- `--neutral-*`: queued, pending, unknown, or ordinary metadata. The shared neutral component treatment is `--neutral-fg` (`#d4dbea`) on `--neutral-bg` (`#283346`) with `--neutral-border` (`#7486a4`). The border measures 3.44:1 against the neutral fill, 4.32:1 against `--surface-raised`, 4.77:1 against `--surface`, and 5.16:1 against `--canvas`; neutral text measures 9.15:1 against its fill. `.node`, unmodified `.badge`, and `.state` consume this same treatment, so neutral graph nodes, queued or unknown badges, and metadata capsules retain a boundary that independently meets the 3:1 meaningful-component/graphic target.
- `--active-*`: running and in-progress lifecycle states.
- `--success-*`: completed, passed, or published states.
- `--warning-*`: blocked, waiting, or needs-attention states.
- `--danger-*`: failures, errors, and destructive actions.

Each semantic family has foreground, background, and border tokens. Never select a semantic color for decoration. Every lifecycle state still needs readable text and, where useful, a consistent non-color marker. The neutral capsule contract is distinct from the broader `--border` operational-surface contract: `--neutral-border` defines the perimeter between a compact neutral component, its own fill, and surrounding canvas/surface/raised-surface backgrounds, while `--border` separates panels, repositories, runs, and dividers from adjacent surfaces. Do not substitute one contract for the other or introduce badge- or node-specific border colors; if a new compact neutral pattern has the same meaning and adjacencies, reuse all three `--neutral-*` tokens and recheck every intended adjacency.

## Reusable patterns

### Bordered surface

`.panel` is the base workflow surface and `.repo` is the raised object surface. Nested run content uses `.run` with a divider instead of another visually heavy card. Avoid nesting multiple shadowed cards.

### Field and form layout

Use `.field-grid` around `.field` groups. Each group contains a real `<label>` or a label-associated structure, `.field-label`, optional `.field-hint`, and a native input. The form becomes one column below its content breakpoint. Required and optional status remains visible and programmatically supported by native attributes.

Inputs and buttons share height, border radius, font, hover behavior, focus treatment, and disabled-state opacity. Primary actions are filled. `.danger` is outlined by default and includes destructive wording, so its meaning does not depend on red alone.

### Focus and interaction states

Links, buttons, inputs, and native summaries share a 3px `:focus-visible` outline with offset. Do not remove native focus unless this shared replacement is present.

Supported states:

| State | Treatment |
|---|---|
| Default | Stable border, legible foreground, control-specific surface |
| Hover | Shared `--accent-hover` surface for enabled primary controls, retaining at least 4.5:1 normal-text contrast; not the only indication that an item is interactive |
| Focus | High-contrast external outline via `:focus-visible` |
| Active | Small button press offset, removed for reduced motion |
| Disabled | Legible reduced emphasis and `not-allowed` pointer; native `disabled` required |
| Pending | Disable only the affected control and change visible label to a pending verb |

The embedded repository form applies this pending treatment to add and remove mutations, including native disabled states and shared mutation guarding.

### Link

All links are visibly underlined without hover. Use links only for navigation. Branches use `.code`; pull-request links retain a meaningful visible number and use safe external-link semantics (`target="_blank"` with `rel="noopener noreferrer"`) in the embedded client.

### Repository and run header

`.repo-head` and `.run-head` place identity and state/action groups at opposite edges while space permits, then stack in DOM order at narrow widths. Repeated actions require contextual accessible names. `.meta` subordinates labeled branch and target information without making it low contrast.

### Graph node

`.graph` wraps an ordered list of node pills. `.node` is metadata, not lifecycle status, and uses the shared neutral foreground, fill, and boundary tokens rather than a graph-specific color. DOM order and visible sequence numbers provide the non-geometric sequence; the circular sequence marker uses `currentColor`, and `.arrow` is an additional visual direction cue. At the narrowest breakpoint, nodes stack and arrows point vertically while the ordered-list semantics and sequence numbers remain intact. Token changes must preserve the neutral text contrast, the sequence marker's legibility, and the 3:1 node perimeter against both its fill and every surrounding surface on which it is rendered.

### Status badge

`.badge` is the base pattern; use one semantic modifier:

- `.badge--active`
- `.badge--success`
- `.badge--warning`
- `.badge--danger`

No modifier means neutral. The embedded status renderer maps machine states to this shared badge family, normalized human-readable labels, and non-color glyphs; unknown states receive a readable neutral fallback. A glyph may accompany the label, but never replace it. Badges are not controls and must not receive hover or pointer styling.

### Feedback

`.feedback` reserves a short, stable feedback line near a workflow. Use `.feedback--error`, `.feedback--warning`, or `.feedback--success` according to meaning. Warning and success treatments combine text, background, and a strong leading border. The semantic element or ARIA role determines announcement behavior; CSS classes alone do not.

Use `.empty` for explanatory empty-state copy and `.skeleton` only for a restrained loading placeholder that preserves expected geometry. Every empty/loading treatment must include real status text.

## Responsive behavior

The base layout is fluid and content-bounded. At 720px, forms, two-column detail, repository headers, and run headers stack. At 400px, surface padding reduces and graph nodes become a vertical sequence. The 16px page gutter and `minmax(0, 1fr)` detail columns prevent page-level horizontal scrolling at 320px. Long names and identifiers wrap.

Breakpoints represent where the current content stops fitting, not named device classes. Test at 320px, 768px, and 1280px and at 200% zoom.

## Accessibility safeguards

- System fonts avoid remote availability and preserve familiar glyph rendering.
- Base text is 16px; supporting text is no smaller than 14px.
- Native controls retain semantic and keyboard behavior.
- All interactive patterns have a visible focus treatment.
- `prefers-reduced-motion` removes the only positional active-state effect and neutralizes future transitions/animations.
- `forced-colors` restores explicit component boundaries and a system-highlight focus outline.
- Status colors are paired with text and borders; the embedded renderer supplies normalized labels and non-color cues.
- Control height is 44px and narrow layouts separate stacked actions.

The selected foreground/background and boundary pairs were checked using WCAG relative luminance math. Normal text pairs exceed 4.5:1. The shared `--border` boundary exceeds 3:1 against `--canvas`, `--surface`, and `--surface-raised`; token changes must preserve that minimum for every adjacent surface the boundary separates. Independently, `--neutral-border` must remain at least 3:1 against `--neutral-bg` and against each surrounding surface the compact capsule perimeter is intended to distinguish, including `--surface-raised` for badges inside runs. Neutral foreground text must remain at least 4.5:1 against its fill, and text labels, glyphs, ordered-list semantics, and sequence numbers must continue to convey meaning without depending on color. Active, success, warning, danger, focus, disabled, responsive, and forced-colors treatments are separate contracts and must not be weakened when neutral tokens change; forced-colors mode continues to use system boundary colors.

## Extension rules

1. Reuse a role token before adding a value.
2. If a new value is necessary across multiple patterns, add a semantic token rather than a component-named token.
3. Use existing type, spacing, and radius scales; do not interpolate arbitrary values.
4. Preserve native semantics before adding ARIA or custom controls.
5. Add interaction styles for keyboard, pointer, disabled, and pending states together.
6. Check every semantic color pair and never encode state by color alone.
7. Verify long content, 320px reflow, 200% zoom, reduced motion, and forced colors.
8. Keep core assets inline/local; remote services may not be required for presentation or interaction.

## Implemented behavior and follow-up boundaries

The embedded client now applies this system to the complete dashboard interaction baseline. It includes normalized, text-and-glyph lifecycle statuses; an explicitly ordered and numbered agent graph; contextual destructive confirmation; guarded pending add and remove mutations; accessible validation and workflow feedback; targeted status and alert regions; responsive repository and run detail; and refresh behavior that retains the last valid state, avoids unchanged DOM replacement, and preserves relevant focus. These capabilities are implementation-owned by `repogents/http_api.py` and protected by UI-facing regression coverage in `tests/test_http_api.py`; contributors should refine them in place rather than recreate parallel components or assign them to unspecified downstream work.

Follow-up work is limited to capabilities beyond that baseline or to hardening a behavior when a concrete defect is identified. Examples include scale-driven filtering or disclosure for substantially larger run histories, an optional manual refresh control, and a complete user-selectable light theme. Such extensions must continue to use the shared tokens, native semantics, focused announcement regions, retained-content refresh model, and locally served dependency-free architecture. This boundary does not reserve already implemented interaction behavior for a later dashboard, form, status, graph, or live-state layer.
