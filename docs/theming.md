# Custom look for Zulip instances

**Can instances get a custom design on the existing infrastructure — e.g. paper-tiger styling for some, Zulip's default for others?**
Yes, with two complementary layers. Neither is built yet; this is the finding and the plan.

## What Zulip supports natively (per realm, no infrastructure change)

An organization owner sets these in Zulip (Settings → Organization → Profile):
- **name and description**;
- **icon**, and a **logo** in light and dark variants;
- the **default theme** (light/dark/automatic) for new users.

They are per realm, so on a shared server each group can have its own. They survive upgrades because they are product features.

For paper-tiger, that means the ORFE logo (`assets/logos/orfe-logo.png`), the Princeton shield as icon, and the realm name. It could be scripted through the `-mgmt` job (a `chat-manage set-branding` command uploading those files), or done once by hand.

Server-wide there are also:
- `INSTALLATION_NAME` (shown in email and on the login page);
- `CUSTOM_LOGO_URL`;
- custom policy pages (`POLICIES_DIRECTORY`) and a terms-of-service gate.

## What needs our layer: colours and type

Zulip has no supported custom-CSS setting. Its web app, however, is styled through CSS custom properties, so overriding tokens re-skins it without touching its markup. Examples:
- `--color-background`, `--color-text-default` and `--color-link`;
- `--color-outline-focus` and `--color-background-zulip-button`;
- about 470 `--color-*` properties in 12.3.

**Verified against Zulip 12.3 in the e2e stack (2026-09-30):**
- Zulip sends **no Content-Security-Policy** on its pages, so a same-origin stylesheet is allowed.
- HTML comes from Django **uncompressed**, and nginx has `ngx_http_sub_module`. A server-level `sub_filter` in the `app.d` include (the one the `/<slug>` redirects already use) injected `<link rel="stylesheet" href="/chat-theme/…css">` into `/login/`, `/accounts/login/` and `/`.
- The stylesheet itself is served from `/home/zulip/local-static/` (Zulip's own `/local-static` path, or an `app.d` location).
- Dark mode is Zulip's `.dark-theme` / `.color-scheme-automatic` classes, with the values written as `light-dark(...)`. A theme must give dark values too. paper-tiger has none, so they would be derived (e.g. brand orange `#e77500` with darker neutrals).

## The plan

1. **Themes live in the template image:** `image/themes/<name>/theme.css`, plus self-hosted fonts and logos. `paper-tiger` would map its tokens onto Zulip's variables:
   - `--siempre-accent` → Zulip's accent, link and focus colours;
   - its neutrals → backgrounds and text;
   - Source Sans 3 / Source Serif 4 (Google Fonts, or self-hosted).

   Adobe's `sofia-pro` would need the chat hostnames added to the Typekit kit's allowed domains; otherwise the Source fallbacks apply.
2. **Selection in `chat.yml`:**
   - `theme: paper-tiger` on a server applies to all its realms;
   - `themes: {<slug>: paper-tiger}` on a shared server applies per realm;
   - absent means Zulip's default.

   `render.py` passes a validated `host → theme` map. `chat-entrypoint` writes an http-level `map $host $chat_theme {…}` and the server-level `sub_filter` that links `/chat-theme/$chat_theme.css`, only for hosts with a theme. It builds these from names it checks against the themes baked into the image, never from text, like the rate limits.
3. **Tests:**
   - a unit test for the generated nginx config;
   - an e2e assertion that a themed host's page links the stylesheet, an unthemed host's page doesn't, and the CSS is served;
   - a screenshot check (Playwright) of the login page and the app, so a Zulip upgrade that renames variables shows up in CI rather than in production.

## Trade-offs

- **Upgrades:** the variable names are Zulip internals, not an API. Themes should override only the token layer (variables), never structural selectors, so a Zulip upgrade degrades gracefully: unmatched variables simply fall back to Zulip's defaults. The screenshot check catches drift.
- **Mobile apps:** Zulip's iOS/Android apps are native and ignore web CSS. They show the realm's icon and name, which come from the native layer above.
- **Emails** use Zulip's own templates; only `INSTALLATION_NAME` and the realm name/logo carry over.
- **What not to take from paper-tiger:** its component classes (`.pt-*`), and its global element rules (the orange `h2::after` bar, forced `box-sizing`, serif body text), which would fight Zulip's layout. Take the tokens and the logos only.
