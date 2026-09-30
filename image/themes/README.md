# Themes

Each directory is one theme a `chat.yml` can name (`theme: <name>` on a server, or per
realm on a shared server). `theme.css` is required; anything else in the directory is
served beside it at `/chat-theme/<name>/`.

Rules, so Zulip upgrades stay safe:
- **Override Zulip's CSS custom properties only** (`--color-*`), never its selectors or
  markup. A renamed variable then just falls back to Zulip's default.
- **Give light and dark values** with `light-dark(<light>, <dark>)`: Zulip sets
  `color-scheme: dark` on `:root` in dark mode.
- **No third-party requests** (fonts, images). Themes run on every page, and preview
  hosts (`*.azurecontainerapps.io`) are not covered by domain-locked font licences.
- **No trademarked assets in this public repo.** Realm logos and icons are uploaded per
  realm in Zulip (Organization settings → Profile).

`tests/e2e` checks that every variable a theme sets exists in the running Zulip.
