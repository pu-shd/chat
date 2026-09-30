# Custom look for Zulip instances

Instances can have a custom look (e.g. Princeton styling) while others keep Zulip's default. There are two layers.

## 1. Zulip's own branding (per realm, set in Zulip)

An organization owner sets these in Zulip (Settings → Organization → Profile):
- **name and description**;
- **icon**, and a **logo** in light and dark variants;
- the **default theme** (light/dark/automatic) for new users.

They are per realm, so on a shared server each group can differ. They are product features, so they survive upgrades, and they also show in the mobile apps.

Logos are deliberately **not** shipped in this public repo: Princeton marks are trademarks, and the design kit they come from is private. Upload them per realm.

## 2. Colour themes (`theme:` in `chat.yml`)

```yaml
servers:
  dept:
    kind: dedicated
    host: chat.<dept>.princeton.edu
    theme: paper-tiger            # this server's realm
  groups:
    kind: shared
    theme: paper-tiger            # every realm on it…
    realms:
      - {slug: example-group, …}
      - {slug: beta-lab, …, theme: default}   # …except this one: Zulip's own look
```

Themes ship in the image under `image/themes/<name>/theme.css`. Available now:

| Theme | Look |
|---|---|
| `paper-tiger` | Princeton orange from the "Paper Tiger" design kit (accent `#e77500`, ink `#333`), with light and dark values |

**What the theme recolours:** primary/secondary buttons, banners, links, the focus ring, mentions of you, input pills, sidebar hover, the unread marker, an orange rule under the navbar, and the login/sign-up pages' buttons, links and footer. Everything else stays Zulip.

**How it works:**
- `render.py` validates the theme names against the image and turns them into `host=theme` pairs. The pairs cover each realm's live hostname, plus the pre-DNS `*.azurecontainerapps.io` name for the realm that answers there.
- The image's entrypoint turns the pairs into an nginx `map $host` plus a `sub_filter` that adds one same-origin `<link rel="stylesheet" href="/chat-theme/<name>/theme.css?v=<hash>">` before `</head>`.
  - Zulip sends no Content-Security-Policy, and its HTML is not compressed upstream, so this is all it takes.
  - The stylesheet is served with Zulip's own security headers.
  - Malformed pairs, or a theme that isn't in the image, switch themes off with a log line; they never break nginx.
- A theme overrides **only Zulip's CSS custom properties**, never its markup. Zulip's web app and its login pages are driven by about 640 `--color-*` variables, and Zulip sets `color-scheme: dark` in dark mode, so `light-dark(<light>, <dark>)` values follow each user's choice.

**Safety against Zulip upgrades:**
- A renamed variable just falls back to Zulip's default.
- The e2e suite checks, against the running Zulip, that **every variable a theme sets still exists** in its CSS bundles. It also checks that themed pages (login, and the signed-in app) link the stylesheet, and that it is served.
- Unit tests enforce the theme rules:
  - `:root` / `::selection` rules only;
  - no `url()` or `@import` (no third-party requests);
  - dark values present.

## Adding a theme

Copy `image/themes/paper-tiger/` to a new name and change the `--pt-*` palette at the top. Preview it before releasing with `scripts/local.zsh up --config <dept> --theme <name>`, which builds the image from your checkout (see the README, "Looking at it locally"). Then release the template; config repos select it with `theme: <name>`. The rules are in `image/themes/README.md`.

## What does not change

- **The iOS/Android apps** are native: they show the realm icon and name (layer 1), not the colours.
- **Emails** use Zulip's templates.
- **Fonts:** themes load none. Zulip already ships Source Sans 3, Paper Tiger's own fallback. Adobe's sofia-pro is licensed only for `*.princeton.edu`, and would add a third-party request on every page.
