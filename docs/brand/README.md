# Swarm Code Factory Brand Guidelines

Swarm Code Factory: From specification to shipped software. The identity is deliberately **warm and industrial**:
molten ember on cool graphite steel, with a bright struck spark for moments of
focus. It is intentionally *not* the cool indigo/gray of Linear-style tools.

---

## 1. The mark

Until new artwork exists, the mark is plain text: **SCF** set in `--font-mono`,
bold, in `--primary-foreground` on a small `--primary` square, followed by the
full name **Swarm Code Factory** in `--font-display`. It is rendered in
`apps/web/src/components/app-shell.tsx` from the design tokens, so it re-themes
in light and dark. There are no logo image files and no favicon.

### Do / Don't

**Do**
- Write the full name, Swarm Code Factory, next to the SCF mark the first time it appears.
- Use the design tokens for its colours, so it follows the theme.

**Don't**
- Draw a pictorial mark or a wordmark image until the new identity is designed.
- Recolour the mark outside the palette.

---

## 2. Colour tokens

Tokens live in `apps/web/src/app/globals.css` (`:root` + `.dark`) and are wired
into Tailwind in `apps/web/tailwind.config.ts`. They are stored as bare
`H S% L%` triples and consumed via `hsl(var(--token))`. Every colour has a light
("cooled steel") and dark ("graphite at night") value.

### Brand

| Token | Role | Light | Dark |
| --- | --- | --- | --- |
| `--primary` | **Ember** — hero accent | `#F0611A` · `hsl(20 88% 52%)` | `#F76C1C` · `hsl(22 90% 55%)` |
| `--spark` | **Spark** — bright struck highlight | `#FFB13D` · `hsl(36 100% 62%)` | `#FFBF52` · `hsl(38 100% 66%)` |
| `--ring` | Focus glow (spark-toned) | `hsl(36 100% 55%)` | `hsl(36 100% 58%)` |

The ember carries **dark warm text** (`--primary-foreground`, `hsl(24 45% 12%)`)
for accessible, high-punch "hot-stamped" contrast on buttons.

### Neutrals — cool graphite steel

| Token | Light | Dark |
| --- | --- | --- |
| `--background` (bg) | `hsl(210 20% 98%)` | `hsl(220 26% 8%)` |
| `--card` / `--popover` (surface) | `hsl(0 0% 100%)` | `hsl(220 22% 11%)` |
| `--secondary` / `--muted` (surface) | `hsl(214 20% 94%)` | `hsl(217 16% 18%)` |
| `--border` / `--input` | `hsl(214 22% 88%)` | `hsl(216 16% 22%)` |
| `--foreground` (text) | `hsl(215 32% 15%)` | `hsl(210 18% 90%)` |
| `--muted-foreground` (text) | `hsl(215 16% 42%)` | `hsl(214 15% 62%)` |
| `--accent` (warm hover surface) | `hsl(26 60% 94%)` | `hsl(22 45% 16%)` |
| `--accent-foreground` | `hsl(18 65% 32%)` | `hsl(30 90% 72%)` |

`--accent` is a **warm faint hover surface** (metal warming up), not a second
brand hue — hover/active states glow slightly amber.

### Status

| Token | Role | Light | Dark |
| --- | --- | --- | --- |
| `--success` | success | `#22A45F` · `hsl(150 58% 40%)` | `hsl(150 52% 46%)` |
| `--warning` | warning | `#EB9A0F` · `hsl(38 92% 48%)` | `hsl(38 92% 55%)` |
| `--danger` / `--destructive` | error / destructive | `#E63A2E` · `hsl(2 78% 52%)` | `hsl(2 74% 55%)` |

Tailwind exposes these as `bg-success`, `bg-warning`, `bg-danger`, `text-spark`,
etc. (alongside the existing `primary` / `destructive`).

---

## 3. Type

No runtime CDN/network font dependency — the display + mono faces are
**self-hosted via [`@fontsource`](https://fontsource.org)** (their woff2 files
are bundled by Next.js/Turbopack at build time, so the app stays offline-safe),
and the body stays a neutral **system-font stack** (see
`--font-sans` / `--font-display` / `--font-mono` in `globals.css`, wired to
Tailwind's `font-sans` / `font-display` / `font-mono`).

| Role | Token | Stack (first choices) | Voice |
| --- | --- | --- | --- |
| Display / headings | `--font-display` | **Bricolage Grotesque** (self-hosted OFL) → Avenir Next → Futura → Century Gothic → system | Distinctive geometric grotesk, tight, engineered |
| Body / UI | `--font-sans` | system-ui (SF / Segoe UI / Roboto) | Clean, neutral, readable |
| Code / keys | `--font-mono` | **JetBrains Mono** (self-hosted) → SF Mono → Menlo → Consolas | Precise, technical |

The display face is now **Bricolage Grotesque**, a distinctive OFL geometric
grotesk vendored offline via `@fontsource-variable/bricolage-grotesque`
(CSS family `"Bricolage Grotesque Variable"`). Code + keys use self-hosted
**JetBrains Mono** via `@fontsource-variable/jetbrains-mono` (CSS family
`"JetBrains Mono Variable"`). Both are imported in the root layout so their
woff2 assets are bundled and served from the app — no Google Fonts `<link>`.

Headings use `--font-display` with `letter-spacing: -0.02em` (set in the base
layer). Body inherits `--font-sans`.

### Type scale (Tailwind)

| Class | Size | Use |
| --- | --- | --- |
| `text-xs` | 12px | Meta, keys, counts |
| `text-sm` | 14px | Body, table + card text |
| `text-base` | 16px | Default paragraph |
| `text-lg` | 18px | Section titles, brand lockup |
| `text-xl`–`text-2xl` | 20–24px | Page headings |
| `text-3xl`+ | 30px+ | Marketing / hero |

---

## 4. Usage principles

- **Ember is precious.** One primary action per view. If everything is ember,
  nothing is.
- **Steel does the work.** Structure, surfaces and text are cool graphite; the
  ember only marks intent (primary actions, active nav, the brand).
- **Spark = focus.** Reserve the bright spark tone for focus rings, so keyboard focus always reads as "struck".
- **Warm hovers.** Hover/active surfaces warm toward amber (`--accent`) rather
  than going cooler — the interface "heats up" under the cursor.
