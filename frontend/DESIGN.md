# Arya builder design system

The builder, the share-link page and the embeddable widget follow the Arya landing site (repo `arya-landing-designs`, live at aryawebsitedesigns.vercel.app). This file is the record of what was copied, what was derived, and the rules that keep the three surfaces reading as one product. The stylesheet is `src/styles.css`; tokens sit at its top, dark tokens under the DARK THEME banner.

## Decisions

1. **Type**: Geist for everything, Geist Mono for keys, snippets and ids, self-hosted through `@fontsource-variable` and registered as `"Geist Variable"` / `"Geist Mono Variable"` (the site inlines Google's static Geist as `'Geist'`; same face). Scale as tokens: `--fs-h1 40px` (52px on Home only), `--fs-h2 24px`, `--fs-h3 17px`, `--fs-body 15px`, `--fs-small 13px`, `--fs-meta 12.5px`. Headings weight 400 to 500, tracking -0.035em on h1, sentence case, no trailing period on page titles, `text-wrap: balance`. Body uses the site's `font-feature-settings: "ss01", "cv11"`.
2. **Colour**: green is the one accent. `--accent` (fill), `--accent-strong` (hover fill), `--accent-ink` (green as text, borders, marks). Status green merges into the brand ramp; red and warn take the site's invalid and error reds.
3. **Surfaces**: paper canvas, no gradients. Panels white with a `--line` hairline. Selected and hover states use the sage wash and a green hairline, never a lift. Shadows only on floating things: dialogs, popovers, the widget panel, the orb glow.
4. **Radius**: `--r-sm 10px` inputs and chips, `--r-md 18px` cards, notes, dialogs, `--r-lg 22px` feature cards and the Preview stack, `--r-pill 30px` buttons and tags.
5. **Buttons**: the site's `.primary` / `.secondary`: pill, sentence case, 14px / 500, 12px 21px. `.btn` primary green, `.btn.ghost` white with hairline and a 1px shadow, `.btn.danger` red, `.btn-link` inline. `.btn.on-lime` and `.btn.on-green` for the two bands.
6. **Labels**: `.label` is the site's field label (13px / 600, sentence case) with `.label-opt` for an "Optional" suffix. Uppercase survives only in `.eyebrow` and the places that act as one (stat labels, table heads, map kickers, menu headings).
7. **Accent moments**: the lime band (`.card.primary`) for the one primary action on a screen; the green band (`.band.green`) for Deploy; industry tints (`.card-tint-*`) on saved-agent cards under a 55% white veil so text stays AA; the Preview stack in sand, sage, sky.
8. **Motion**: the orb, dialog fade and widget morph stay; no card hover translate. Focus is the site's ring: 2px green outline, 3px offset.
9. **Brand**: the hexagonal mark (`BrandMark.jsx`, currentColor) plus the lowercase "arya" wordmark on builder screens; mark plus agent name on the call screen. Favicon set from the site's `brand/`.
10. **Icons**: lucide (same stroke language as the site's Phosphor set).
11. **Dark mode**: green-cast near-black paper, green fill one step lighter so white holds, green-as-text a mint, lime a dim olive band with a pale lime label. Builder-only; the site has no dark mode.
12. **Orb and launcher**: default to a mid sage (`--orb-hi #6f9d82`), not the brand green, which mixes to mud through the gradient stops. Customers retint via `?accent=`.

## Token parity with the site

| Builder token | Value | Site source |
|---|---|---|
| `--paper` | `#fcfcfc` | base `:root --paper` |
| `--ink` | `#111111` | `landing.css:2 --ink` |
| `--muted` | `#626262` | base `:root --muted` |
| `--line` | `#e7e7e7` | base `:root --line` |
| `--line-2` | `#d9ddd3` | `landing.css:269 .nav-github` border |
| `--green` / `--accent` | `#245444` | `landing.css:2 --green` |
| `--green-ink` / `--accent-ink` | `#1b3d31` | `landing.css:2 --green-ink` |
| `--lime` | `#e4f3b5` | `landing.css:2 --lime` |
| `--on-lime-soft` | `#33503f` | `landing.css:89 .usecase-scratch p` |
| `--on-green-soft` | `#d3e2cf` | `landing.css:367 .pilot-head p` |
| `--field` / `--field-line` | `#fbfbf8` / `#d8dbd2` | `landing.css:109 .field input` |
| `--code-bg` / `--code-line` | `#f4f6f0` / `#dfe3d9` | `landing.css:339 .run-block` |
| `--sage` / `--sage-line` | `#eef2e6` / `#dfe7d3` | `landing.css:95, 289` |
| `--sand` | `#ece5d4` | base hero card |
| `--red` / `--red-ink` | `#b3412e` / `#a33a29` | `landing.css:114, 115` |
| `--accent-soft` | `rgba(36,84,68,.125)` | `landing.css:113` (`#24544420`) |
| `--r-sm`, `--r-md`, `--r-pill` | 10 / 18 / 30px | `landing.css:133` |
| `--fs-meta` | 12.5px | `landing.css:133` |
| Stepper | 38px, `#d3d6cd`, `#73766e`, `#555` | `landing.css:56-68` |
| Tints | six gradients | base `.voice-card` variants |

Everything else is derived and documented in the token comments.

## Rules

- Never hardcode a colour in JSX; the only CSS variables JSX may touch are `--level`, `--orb-accent` and `--i`.
- The widget imports this stylesheet whole. Do not rename `.callframe`, `.callroom`, `.messages`, `.input-area`, `.bigorb`, `.heroname`, `.herostate`, `.pm-row`, `.pm-bub`, `.link-chip*`, `.srccard*`, `.optcard*` or `.wgt*`, and keep `--level`, `--orb-size`, `--orb-accent`.
- `src/loader/index.js` is token-less: its `DEFAULT_ACCENT`, panel colours and gradient anchor mirror `--orb-hi`, `--panel`, `--paper` and must be synced by hand.
- Nothing under `src/call/` or the components the widget imports may import `src/api.js` (build guard).
- Contrast targets: 4.5:1 for text, 3:1 for glyphs on fills. Ratios in the token comments are against the surface named.
