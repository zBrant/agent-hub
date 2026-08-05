# Design system

**Base:** shadcn/ui on [Base UI](https://base-ui.com) + Tailwind v4.
**Charts:** [Tremor](https://www.tremor.so/), with its palette overridden by the tokens here.
**Icons:** `lucide-react`.
**Theme:** dark-only, high density.

Visual references: Linear (density, keyboard), Sourcegraph Wildcard (code-tool
vocabulary), Vercel Geist (surfaces and neutrals).

---

## 1. Principles

1. **Dense.** This is an operations app, not a landing page. People keep it open
   all day and need to see twelve nodes and system health without scrolling. When
   torn between breathing room and information, choose information.

2. **Dark-only.** One theme. The terminal is already dark and so is the graph; a
   light theme would double the token work across the project for a use case that
   doesn't exist. The semantic-token discipline (§2) still applies, so adding light
   later stays possible — but don't build for it now.

3. **State is never color alone.** Every node state is **color + icon + label**.
   Color blindness aside, the graph has 90px nodes where the difference between
   amber and red doesn't survive zooming out.

4. **Monospace is semantic.** File paths, symbols, branches, commands, IDs, and
   diffs are mono. Interface text is sans. If it's mono, the user can copy it
   somewhere useful.

5. **Nothing animates during a stream.** Text arriving from an agent does not
   fade in. Animating content that updates 30×/s is noise and costs frames.

---

## 2. Tokens

Zero raw color values in components. If you wrote `#` or `rgb(` outside this file,
it's a bug.

```css
/* frontend/src/styles/tokens.css */
@theme {
  /* surfaces */
  --color-bg:            #0B0C0E;   /* application background */
  --color-surface:       #121417;   /* card, panel, sidebar */
  --color-elevated:      #191C20;   /* drawer, popover, menu, dialog */
  --color-inset:         #08090A;   /* terminal, code block, diff */

  /* borders */
  --color-border:        #24282E;
  --color-border-strong: #333941;   /* panel divider, inactive focus ring */

  /* text */
  --color-fg:            #E6E8EB;
  --color-fg-muted:      #9BA1A9;   /* labels, metadata, timestamps */
  --color-fg-subtle:     #6B7280;   /* placeholder, disabled text */

  /* action */
  --color-accent:        #4C8DFF;
  --color-accent-hover:  #6BA0FF;
  --color-accent-fg:     #0B0C0E;
  --color-focus:         #4C8DFF;

  /* state */
  --color-pending:       #6B7280;
  --color-ready:         #4C8DFF;
  --color-running:       #38BDF8;
  --color-review:        #F5A524;
  --color-blocked:       #F97066;
  --color-done:          #3DD68C;
  --color-failed:        #E5484D;
  --color-skipped:       #4B5563;

  /* typography */
  --font-sans: "Inter", ui-sans-serif, system-ui, sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;

  /* radius */
  --radius-sm: 4px;
  --radius-md: 6px;
  --radius-lg: 8px;
}
```

**Naming rule:** tokens are semantic (`--color-blocked`), never literal
(`--color-red-400`). The name describes the role, not the pigment.

---

## 3. Typography

| Use | Size | Weight | Family |
|---|---|---|---|
| Page title | 18px | 600 | sans |
| Section / card title | 13px | 600 | sans |
| Body / UI | **13px** | 400 | sans |
| Label, metadata, timestamp | 12px | 400 | sans, `fg-muted` |
| Badge, counter | 11px | 500 | sans |
| Code, path, branch, ID | 12px | 400 | **mono** |
| Terminal | 12.5px | 400 | mono, `line-height: 1.35` |

13px as the base is deliberate — 16px costs ~20% of vertical density. There are no
sizes outside this table.

Line height: 1.45 for UI text, 1.35 in the terminal and code blocks, 1.6 for long
prose (agent transcripts, code-search answers).

---

## 4. Spacing and dimensions

Scale: **4, 6, 8, 12, 16, 24, 32**. Nothing outside it.

| Element | Height |
|---|---|
| Top bar | 44px |
| List row (session, process, file) | 28px |
| Input, select | 30px |
| Button `sm` / `default` | 26px / 30px |
| Tab | 32px |
| Graph node | 40px collapsed / auto expanded |

| Panel | Width |
|---|---|
| Session sidebar (Tab 2) | 240px, resizable 200–360 |
| Graph panel (Tab 2, minimap) | 340px, resizable 280–520 |
| Node drawer | 480px, resizable up to 60vw |
| Code panel (Tab 3) | 45% of width |

Padding: 12px in cards and panels; 8px horizontal in list rows; 16px at the page
edge. Gap between cards: 12px.

Use `react-resizable-panels` (shadcn's `Resizable`) for every panel divider, and
persist sizes to `localStorage`. People who use this all day adjust it once.

---

## 5. Node states

The source of truth for color and icon. Do not redefine these in any component.

| State | Token | Icon (lucide) | Label |
|---|---|---|---|
| `pending` | `pending` | `Circle` | Pending |
| `ready` | `ready` | `CircleDot` | Ready |
| `running` | `running` | `Loader` + progress ring | Running |
| `awaiting_review` | `review` | `Eye` | Awaiting review |
| `blocked` | `blocked` | `AlertTriangle` | Blocked |
| `done` | `done` | `Check` | Done |
| `failed` | `failed` | `X` | Failed |
| `skipped` | `skipped` | `SkipForward` | Skipped |

`blocked` and `failed` are both reddish on purpose — they are semantic neighbors —
and are distinguished by icon and shape, not hue. That is exactly the case
principle §1.3 exists for.

Applied to a graph node: 1.5px border in the state color, `surface` background,
14px icon left of the title, harness badge on the right. A `running` node gets an
animated ring (the app's only continuous animation). A selected node gets a 2px
`accent` border.

Edges: `border-strong` normally; `accent` when connected to the selected node;
dashed when the parent hasn't completed.

---

## 6. Harness and model

Harness badge: 11px, mono, `bg-elevated`, `border` outline, `sm` radius, with a 6px
colored dot on the left. Harness color is **identity, not state** — keep it far
from the state palette:

| Harness | Dot |
|---|---|
| `claude-code` | `#D97757` |
| `codex` | `#A0A0A0` |
| `opencode` | `#7C6BF5` |
| others | `--color-fg-subtle` |

The model is always mono and spelled out (`claude-opus-5`), never abbreviated. The
user is making a cost decision — abbreviating hides the information that matters.

---

## 7. Terminal (xterm.js)

The xterm theme is **derived from the tokens**, not written separately:

```ts
// frontend/src/components/terminal/theme.ts
export const terminalTheme = {
  background: token("--color-inset"),
  foreground: token("--color-fg"),
  cursor:     token("--color-accent"),
  selectionBackground: "#4C8DFF33",
  black: "#0B0C0E",  red: "#E5484D",  green: "#3DD68C",  yellow: "#F5A524",
  blue:  "#4C8DFF",  magenta: "#B87BF5", cyan: "#38BDF8", white: "#E6E8EB",
}
```

`fontFamily` = `--font-mono`, `fontSize` 12.5, `lineHeight` 1.35, `scrollback`
5000, addons `fit` + `webgl`.

The terminal container uses `bg-inset`, 8px padding, and **no** inner border
radius — rounding a terminal corner clips characters.

---

## 8. Component inventory

**From shadcn (install, don't rewrite):** Button, Input, Textarea, Select, Dialog,
Sheet, Tabs, Tooltip, DropdownMenu, Badge, Separator, ScrollArea, Command,
Progress, Skeleton, Table, Collapsible, Resizable, Sonner (toast).

`pnpm dlx shadcn@latest add <component>` — after that the code is yours and lives
in `src/components/ui/`. Tune the density (§4) there once, not per call site.

**Custom (`src/components/<domain>/`):**

| Component | Role |
|---|---|
| `GraphCanvas` | `@xyflow/react` + ELK layout. The backend never sends coordinates |
| `GraphNode` | Custom node, `React.memo`, states from §5 |
| `NodeDrawer` | Content by state (`design.md` §8) |
| `EventFeed` | Channel A stream, virtualized past ~200 rows |
| `TerminalPane` | xterm mounted by ref, **never** re-rendered |
| `SessionListItem` | Title, `7/12` progress, harnesses, tokens, elapsed, badge |
| `TokenBar` | Stacked bars by model/harness (Tremor) |
| `SystemGauge` | CPU/RAM/disk, ring buffer, updated over WS |
| `HarnessBadge` / `StatusDot` / `ModelSelect` | §5 and §6 |
| `CodeRef` | Clickable `path/to/file.py:123`, mono, opens the side panel |
| `DiffView` | Worktree diff on `bg-inset` |

**Rule:** a new component is born only when the same arrangement appears a third
time. Before that it's inline composition in the route.

---

## 9. Charts (Tremor)

Override Tremor's default palette with the tokens. Rules:

- No gradients, no shadows, no 3D.
- Grid in `border` at 40% opacity; axes in `fg-subtle`, 11px.
- Tooltip on `bg-elevated` with a `border` outline, values in mono.
- Token counts on the Y axis always use compact suffixes (`412K`, `1.2M`), never
  raw numbers.
- Cost always carries the **"estimated equivalent"** label when the session runs
  under a subscription (`CLAUDE.md`, invariant 7). That is a product rule, not a
  style rule.
- Stacked token bars use a fixed order: `cache_read` → `cache_write` → `input` →
  `output`, cheapest to most expensive. The order communicates cost proportion.

---

## 10. Motion

| What | Duration | Curve |
|---|---|---|
| Hover, focus, color | 120ms | `ease-out` |
| Drawer, dialog, panel | 180ms | `cubic-bezier(0.2, 0, 0, 1)` |
| Graph layout (ELK) | 300ms | `ease-in-out` |
| `running` node ring | 1.4s loop | `linear` |

Nothing else animates. Respect `prefers-reduced-motion: reduce` by disabling
everything except the progress ring, which is state information rather than
decoration.

---

## 11. Accessibility

- Contrast ≥ 4.5:1 for text. `fg-subtle` (#6B7280 on #0B0C0E ≈ 4.6:1) is the floor
  — don't create anything dimmer.
- Focus is always visible: `outline: 2px solid var(--color-focus); outline-offset: 2px`.
  Never `outline: none` without a replacement.
- State = color + icon + an `aria-label` carrying the written label (§5).
- `⌘K` opens the command palette. Every primary action — switch session, jump to a
  node, run, interrupt, search the code — is reachable from the keyboard.
- The graph is Tab-navigable between nodes, with `aria-label` describing state and
  dependencies. A bare canvas without this is inaccessible.
- `EventFeed` is `aria-live="polite"` with throttling — don't announce every delta.

---

## 12. Setup

```bash
cd frontend
pnpm create vite@latest . --template react-ts
pnpm add tailwindcss @tailwindcss/vite
pnpm dlx shadcn@latest init          # choose Base UI
pnpm add @tremor/react lucide-react @xyflow/react elkjs \
         @xterm/xterm @xterm/addon-fit @xterm/addon-webgl \
         @tanstack/react-query @tanstack/react-virtual zustand \
         react-router react-resizable-panels
```

`src/styles/tokens.css` is imported before everything else. The `globals.css`
generated by shadcn maps its variables (`--background`, `--foreground`,
`--primary`…) onto the §2 tokens — a mapping, not a second palette.
