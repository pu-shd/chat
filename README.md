# pu-shd/chat

Vend [Zulip](https://zulip.com) chat workspaces on Azure for Princeton departments and research groups.
This public repo is the **template**: infrastructure, image, scripts, and reusable workflows.
Each department keeps a small **private config repo** (for example `pu-<dept>/chat-config`) that holds one `chat.yml` and deploys through GitOps.
It follows the same pattern as `page-stream` / `page-stream-config`.

```
pu-<dept>/chat-config (private)              pu-shd/chat (public, this repo)
├── template.lock  ── pins ref + commit sha ─▶ release vX.Y.Z (commit)
├── <dept>/chat.yml  ── tools/render.py ────▶ <dept>/generated/*.json   (committed, CI-checked)
└── .github/workflows/deploy.yml ── uses ───▶ build-image.yml@sha   → az acr build into the
                                                                       department's registry
                                              deploy-server.yml@sha → infra/server.bicep
```

## What gets deployed

| Layer | Per | Resources (`infra/`) |
|---|---|---|
| platform | department | resource group, VNet, Container Apps environment (workload profiles), PostgreSQL 17 Flexible Server (private), Premium NFS storage, Key Vault (RBAC), **Azure Container Registry** (Basic) for the department's images, Log Analytics |
| server | Zulip server | Container App with **exactly one replica**: `zulip` plus `redis`, `memcached` and `rabbitmq` sidecars, all pulled from the department registry. Also an NFS `/data` share; the jobs `<app>-dbinit` (database and role), `<app>-mgmt` (realms, mail test, push registration) and, with Healthchecks, `<app>-hc`; and two managed identities, `<app>-id` and `<app>-db-id` (see Security model) |
| realm | Zulip organization | a row in a server's database, created by the `-mgmt` job |

**Sign-in** is Zulip's own **Microsoft Entra ID OIDC** login, restricted to the tenant. It works for the web, desktop and mobile apps.
- Each server has its own app registration. User assignment is required on it, so only assigned users or groups can sign in.
- Entra **Easy Auth** is available as an optional perimeter (`easy_auth: true`). It is off by default because it can only guard the web UI: API, uploads, SCIM and mobile sign-in must be exempt from it.

**URLs.** Zulip cannot be served from a URL path, only from the root of a hostname.
- A research group therefore lives at `<slug>.chat.<dept>.princeton.edu`.
- `chat.<dept>.princeton.edu/<slug>` is a 302 redirect to it, served by the department server's nginx.
- The group's URL is the same whether it is:
  - a **realm on the shared `groups` server** (cheap), or
  - a **dedicated server** (isolated, for groups that need their own data and upgrade schedule).

**Email** goes out over SMTP on port 587 (Azure blocks 25), through one of:
- **Resend** (`provider: resend`, the default choice). The API key goes into Key Vault as `email-password`.
- **Azure Communication Services** (`provider: acs`). See Email with Azure Communication Services.
- Any other relay (`provider: smtp`, with `host`, `port` and `user`).

**Request limits** in Zulip's nginx cap each client IP on the sign-in, sign-up and API paths (on by default; see Per-client-IP request limits).

**IP gate (optional, per server; can be switched off for a whole department).** An allowlist of Princeton campus and GlobalProtect VPN ranges, taken from [pugwips](https://github.com/PrincetonUniversity/pugwips).
- **Where the VPN ranges come from, in order:**
  1. Live signed release, when `PUGWIPS_READ_TOKEN` exists.
  2. A **static link**: `ip_gate.fallback_url`, or the dated snapshot committed next to `chat.yml`.
  3. The rules already applied to the app.
- The allowlist is never narrowed below the `prefix_mode` you asked for.
- The gate blocks the mobile apps for anyone off campus and off VPN.

## A department's `chat.yml`

```yaml
department: <dept>
azure: {tenant_id: …, subscription_id: …, region: canadacentral, prefix: <dept>-chat}
github: {repo: pu-<dept>/chat-config, environment: <dept>}
admin_email: chat-admin@<dept>.princeton.edu
email: {provider: resend, from: noreply@<dept>.princeton.edu}
defaults: {dns: pending, cpu: 2.0}
ip_gate: {enabled: false}               # or leave it on and gate individual servers
servers:
  dept:                                  # chat.<dept>.princeton.edu (+ the /<slug> redirects)
    kind: dedicated
    host: chat.<dept>.princeton.edu
    realm: {name: <Department>, owner: {email: …, name: …}}
  groups:                                # <slug>.chat.<dept>.princeton.edu, one realm per group
    kind: shared
    external_host: groups.chat.<dept>.princeton.edu
    realm_domain: chat.<dept>.princeton.edu
    realms:
      - {slug: example-group, name: Example Group, owner: {email: …, name: …}}
```

- The schema is in [`schema/chat.schema.json`](schema/chat.schema.json).
- `tools/render.py render <dept>` validates the file and writes `generated/`. It checks:
  - reserved Zulip subdomains and routes;
  - duplicate slugs and hostnames;
  - Azure name lengths;
  - that gated servers have a fallback and a certificate.
- `render --check` is the CI gate that fails when `generated/` is stale.

## First deployment (bootstrap)

You need:
- Owner on the subscription, and the right to create Entra app registrations;
- `az`, `jq`, `zsh`, `git` and `python3`;
- the Resend API key, or, with `provider: acs`, nothing: `acs-email.zsh` creates the SMTP credentials.

The image is built in Azure (ACR Tasks), so the operator needs no Docker.

```zsh
git clone git@github.com:pu-<dept>/chat-config.git && cd chat-config
git clone https://github.com/pu-shd/chat.git .chat-template && git -C .chat-template checkout "$(jq -r .sha template.lock)"
.chat-template/scripts/setup-venv.zsh
.chat-template/scripts/bootstrap.zsh --config <dept> --set-gh-vars
```

`bootstrap.zsh` is colourful, interactive on a terminal, and resumable. It shows a banner (department, template commit, state file) and numbered steps with timings, and ends with a summary.
- **State:** every step's outcome goes to `.chat-bootstrap/<dept>.state` in the config repo (gitignored).
- **Resuming on a terminal:** a rerun shows the checklist (✓ done, ✗ failed, ↻ stale, · pending) and offers to resume at the first unfinished step, start over, or pick a step.
- **Failures:** an interactive run offers retry, skip or quit. A non-interactive run stops and prints the `--resume` command.
- **Flags:**
  - `--resume` skips finished steps; `--restart` forgets them.
  - `--from STEP` / `--only STEP` pick steps; `--step-by-step` asks before each one.
- **Stale steps:** a step finished with a different template commit shows as stale and runs again.
- **Safety:** every step is idempotent, so rerunning is always safe.

The steps:

| Step | Script | What it does |
|---|---|---|
| `prereqs` | | tools, venv, Azure login, `render --check` |
| `platform` | `deploy-platform.zsh` | resource group, Key Vault, network, environment, PostgreSQL, storage, container registry |
| `image` | `build-image.zsh` | builds `chat:<ref>-<sha7>` in the registry with ACR Tasks from template.lock's commit (this checkout must be exactly that commit), locks the tag, and imports the sidecar images |
| `github` | `setup-github-oidc.zsh` | CI app registration with federated credentials for the Environments `<dept>` and `<dept>-admin`; Contributor on the resource group; Key Vault Secrets Officer. With `--set-gh-vars`, it also runs `setup-github-repo.zsh` (see Security model) and sets the `AZURE_*` repository variables |
| `secrets` | | asks for the Resend key, which goes to Key Vault (with `provider: acs`, runs `acs-email.zsh` instead). Optionally `PUGWIPS_READ_TOKEN` (becomes a GitHub secret) when a server is gated. With Healthchecks enabled, also the ping key (Key Vault and GitHub `HEALTHCHECKS_PING_KEY`) and, optionally, the API key |
| `entra` | `entra-app.zsh` | per server: Zulip's sign-in app registration and redirect URIs; the client secret goes straight into Key Vault |
| `access` | `grant-access.zsh` | per server: its two managed identities, each granted read on only its own Key Vault secrets, plus AcrPull on the registry |
| `servers` | `deploy-server.zsh` | per server; the redirect host goes last |
| `healthchecks` | `healthchecks.zsh --sync` | when enabled and an API key exists: creates the checks with their schedules |
| `smoke` | `smoke.zsh` | realm answers, Entra redirect, `/<slug>` redirects |
| `dns` | `bind-domain.zsh --print` | writes `dns-request-<dept>.md`, the request for your DNS administrators |

Before the first bootstrap, cut a template release (`git tag vX.Y.Z && git push --tags`). Copy the release's `template.lock` asset (ref and commit sha) into the config repo, and pin its `uses:` lines to that sha. The weekly Update check does this for later releases.

**Images.** Nothing is published to a public registry. Each department's deploy builds the image into its own Azure Container Registry from the pinned commit (`build-image.zsh`, `az acr build`), the same pattern as graddb and meet.
- The tag `chat:<ref>-<sha7>` is built once and then locked against overwrite and deletion.
- Servers pull it by digest with their managed identities (AcrPull). There are no registry passwords or tokens.
- The sidecar images (redis, memcached, rabbitmq, postgres, curl; see `image/sidecars.json`) are imported into the same registry, so no server depends on Docker Hub, or its rate limits, at run time.

## Running and testing before DNS exists

Every server is `dns: pending` until you say otherwise. A pending server is configured for, and answers on, its Container App name, which Azure already covers with TLS:

```
https://<dept>-chat-dept.<environment-default-domain>/      ← department (root realm)
https://<dept>-chat-groups.<environment-default-domain>/    ← the shared server's preview_realm
```

**What works while pending:**
- Sign-in, the web app, and the desktop and mobile apps (add the server by that URL).
- Email and the `/<slug>` redirects. On the department server, a redirect to a pending server points at that server's preview URL.
- `.chat-template/.venv/bin/python .chat-template/tools/render.py urls <dept> --default-domain <d>` (or the end of `bootstrap.zsh`) lists what is reachable now and where each realm will be once live.

**Limit on the shared server:** only its `preview_realm` is reachable before DNS, because one Container App name maps to one realm. The other group realms are still created; they become reachable when their CNAMEs exist.

## Looking at it locally (no Azure, no Entra)

`scripts/local.zsh` runs one server from a `chat.yml` on your machine, so you can click around a department's workspace (theme, realm name, login page, redirects, mail) before anything exists in Azure. It needs Docker, `docker-compose` and `scripts/setup-venv.zsh`.

```zsh
scripts/local.zsh up --config ../chat-config/<dept>                    # the dept server
scripts/local.zsh up --config ../chat-config/<dept> --server groups    # or one server at a time
scripts/local.zsh up --config ../chat-config/<dept> --theme default    # compare with Zulip's look
scripts/local.zsh status | logs | trust | down
```

- It builds the image from **this checkout**, so uncommitted theme or image changes show up. The settings come from `render.py` exactly as a deploy would produce them. Only the host names change, and the services point at local containers: PostgreSQL (with a non-superuser admin, like Azure's), the sidecars, a mock Entra and a mail sink.
- **Hosts** become `<host>.localhost`, for example `https://chat.<dept>.princeton.edu.localhost/`. macOS resolves every `*.localhost` name to 127.0.0.1 by itself, so `/etc/hosts` needs no edits.
- **Sign-in:** "Log in with Microsoft" goes to a mock Entra at `http://login.localhost:9080` that signs you straight in as the realm owner from `chat.yml`. Nothing contacts Princeton's tenant.
- **Mail** Zulip sends (invitations, for example) shows up at `http://localhost:8025`.
- **TLS** comes from a local CA (Caddy, standing in for Container Apps ingress). Accept the browser warning, or run `local.zsh trust` once to trust that CA in your login keychain; remove it from Keychain Access after `down`. Firefox uses its own certificate store.
- Ports 443, 9080 and 8025 on 127.0.0.1 must be free. `up` is idempotent and keeps data. `down` deletes it.
- Not reproduced locally: the IP gate, Easy Auth, Healthchecks and Azure networking. The shared server's realms each get their own `<slug>.….localhost` host.

## Going live: CNAMEs for DNS

```zsh
.chat-template/scripts/bind-domain.zsh --config <dept> --server dept --print   # ticket text
```

Each hostname needs two records. The values come from the platform, so the ticket can be filed as soon as `deploy-platform.zsh` has run:

| Type | Name | Value |
|---|---|---|
| CNAME | `chat.<dept>.princeton.edu` | `<dept>-chat-dept.<environment-default-domain>` |
| TXT | `asuid.chat.<dept>.princeton.edu` | the environment's `customDomainVerificationId` |

**Records needed for the layout above:**
- **Department:** `chat.<dept>.princeton.edu` → the `dept` app.
- **Shared server, once:**
  - `groups.chat.<dept>.princeton.edu` → the `groups` app;
  - `auth.groups.chat.<dept>.princeton.edu` → the `groups` app. This is the single OIDC callback host for every group realm, so adding a realm needs no Entra change.
- **Each group realm:** `<slug>.chat.<dept>.princeton.edu` → the `groups` app, or → its own app if the group has a dedicated server.
- `chat.<dept>.princeton.edu/<slug>` needs no DNS; it is a redirect.

**Once your DNS administrators confirm the records, for each server:**
1. `bind-domain.zsh --config <dept> --server <name> --wait 30` checks the records, then binds each hostname with a free managed certificate.
   - A server with `ip_gate: true` cannot use a managed certificate, because DigiCert must reach the app. Give it `cert: {key_vault_certificate: <name>}`, for example an InCommon certificate imported into Key Vault.
2. Set `dns: live` for that server in `chat.yml`, then `render`.
3. Run `entra-app.zsh --config <dept> --server <name>` to add the live callback URL. The preview URL is kept until `--prune-redirects`.
4. Commit and push. CI redeploys and Zulip's `EXTERNAL_HOST` switches to the real name.
   - Mobile and desktop users who added the preview URL re-add the real one.

## Adding a research group

- **Realm on the shared server:** append it to `servers.groups.realms` in `chat.yml`, render, then commit and push.
  - CI deploys, and the `-mgmt` job creates the realm with its owner.
  - The owner signs in with Entra.
  - Then ask your DNS administrators for `<slug>.chat.<dept>.princeton.edu` (see `bind-domain.zsh --print`).
- **Dedicated server:** add a `kind: dedicated` server with `host: <slug>.chat.<dept>.princeton.edu` and `slug: <slug>`.
  - Run `entra-app.zsh` and `grant-access.zsh` for it once, then push.
  - The department server's `/<slug>` redirect follows automatically.

Realms are deactivated, never deleted: `realm.zsh --deactivate <slug>`, with type-to-confirm.

## Accounts and roles

**Sign-in is Entra-only; there are no local accounts.**
- `ZULIP_AUTH_BACKENDS` is only `GenericOpenIdConnectBackend`, so:
  - there is no password login, no password reset and no `fetch_api_key` with a password;
  - the login page shows only "Log in with Princeton (Microsoft Entra ID)". The e2e suite asserts that password login is off.
- The mobile and desktop apps sign in through the same Entra flow and then hold a per-user API key. Bots use API keys that their owners create in Zulip.

**Who can sign in at all** is decided in Entra, not in Zulip:
- Each server's app registration requires user assignment. Only users or groups assigned to it (`entra-app.zsh --group <object-id>`, or `entra.allowed_group_id`) get past Entra.
- A new deployment with nobody assigned lets nobody in; `entra-app.zsh` warns about exactly that.

**The first owner, on first deploy.** For each realm, `chat.yml` names an `owner` (email and name).
- The deploy's `-mgmt` job creates the realm with that person as **Organization owner**, Zulip's highest in-app role, with **no password**.
- The first time that person signs in with Entra, Zulip matches the Entra `email` claim to the account (case-insensitive) and they have full rights at once. Use the address Entra actually sends (the user's primary mail), not an alias, or Zulip treats them as a new person.
- The owner is the first person who can:
  - invite people, where needed (on shared servers, sign-up is invitation-only);
  - promote others to administrator or moderator;
  - set the realm's name, logo and icon, and its policies.

**Everyone else:**
- **Dedicated server:** anyone assigned in Entra can create an account on first sign-in (`auto_signup: true`, the default there), as a plain Member.
- **Shared server:** accounts come only from invitations (`auto_signup` defaults to false), because one Entra app admits its users to every realm on that server.

**There is no web "super admin".**
- Zulip's server-level administration is the command line (`manage.py`). Here that is the `-mgmt` job, which only operators with Azure rights can run.
- `admin_email` (`ZULIP_ADMINISTRATOR`) is just the address for server error mail and the support contact shown to users. It is not an account.

**Hand-over and recovery** (the owner has left, or never signed in):
- Use `realm.zsh --config <dept> --server <name> --set-role <slug|_root> <email> <role> [full name]` locally, or the **Operate** workflow's `realm-set-role` action for an existing account.
  - It sets anyone's role (owner, admin, moderator, member, guest).
  - Given a full name, it creates the account first (no password; they sign in with Entra).
  - Granting owner or admin asks for typed confirmation, and in CI runs in the reviewer-gated admin environment.
- Changing a realm's `owner` in `chat.yml` does **not** demote the old owner. `ensure-realm` only creates missing realms; roles in an existing realm change through Zulip or `set-role`.

**If Entra is unavailable**, nobody can start a new session. Existing web sessions and mobile/desktop API keys keep working until they expire or are revoked.

## Security model

- **Who can act as CI.** The config repo's Azure identity trusts two GitHub Environments.
  - `<dept>` (deploy, keepalive, IP gate, update checks) accepts only protected branches, so a pushed feature branch cannot get the Azure token.
  - `<dept>-admin` (Teardown, realm deactivation) also needs a reviewer's approval.
  - `setup-github-repo.zsh` protects `main` and configures both environments. The typed confirmation phrases are a second safeguard, not the only one.
- **What code runs.**
  - Config repos pin every `uses: pu-shd/chat/...` to a **commit SHA**, with the tag as a comment; release tags in pu-shd/chat are immutable (a ruleset).
  - Images are **built inside each department's own registry** from that commit. `build-image.zsh` refuses a template checkout that isn't exactly the locked commit, or that has local changes under `image/`. The built tag is locked against overwrite and deletion. Deploys use its digest and refuse images from any other registry. Zulip's own base image is pinned by digest.
  - Third-party actions are pinned to commit SHAs.
  - The config repo's update check runs new template code only with a read-only token. A separate job, which runs none of it, opens the PR.
- **Least privilege in Azure.** Each server has its own identities, granted per secret by `grant-access.zsh`:
  - `<app>-id`: the Zulip app, the `-mgmt` and `-hc` jobs; only its own secrets.
  - `<app>-db-id`: `-dbinit` only; its database password plus the PostgreSQL admin password.
  - Nothing has vault-wide read, so a compromised server cannot read another server's secrets or the admin password.
  - Both identities have AcrPull on the department registry, and nothing else there. CI builds with its Contributor role on the resource group.
  - Key Vault has purge protection: deleted secrets stay recoverable for 90 days.
- **Secrets never on command lines or in logs.** Key Vault values go through 0600 files. The Healthchecks key reaches curl as config on stdin. psql and Redis read passwords from files. The CI stand-in tests assert this.
- **Inputs are data.**
  - `chat.yml` text fields reject control characters and leading `-`/brackets, and rendered settings are checked before deploy (docker-zulip pastes bracketed values into settings.py as Python).
  - Job arguments that look like options are refused.
  - Workflow inputs are validated per action and passed through `env:`.
  - IP-gate ranges must be strict IPv4 CIDRs no wider than /8. Downloads from `fallback_url` must be signed.
- **Sign-up.** A shared server's Entra app admits everyone assigned to it into *every* realm on it. On shared servers, OIDC `auto_signup` therefore defaults to off (invitation only); give a group that needs a separate audience its own server and Entra group.
- **Proxy trust.** Zulip trusts `X-Forwarded-*` from the whole Container Apps subnet (`LOADBALANCER_IPS`). That subnet includes other apps in the environment, so keep unrelated workloads out of a department's environment.

## Email with Azure Communication Services (optional)

> **Microsoft is retiring ACS Email on 2028-09-30.** Beginning **2026-10-23**, new customers cannot sign up for it; a subscription that already has an ACS resource keeps working until retirement. Treat ACS as a bridge, not a destination. Microsoft's own alternatives are Microsoft 365 **High Volume Email** or Exchange Online, both reachable with `provider: smtp` once the tenant admins provide an account. `render` repeats this warning whenever `provider: acs` is used.

To stop depending on Resend, or keep mail inside the subscription:

```yaml
email:
  provider: acs
  from: donotreply@<dept>.princeton.edu   # custom domain: the only sender until a quota increase
  acs: {domain: <dept>.princeton.edu}     # or {domain: azure-managed}, and no `from`
```

**Sender rules ACS enforces:**
- Mail can come only from configured senders.
  - A new custom domain has just `donotreply@`.
  - Other addresses need Microsoft to approve a sending-quota increase first. Then set `acs.custom_senders: true`, and the platform creates the sender.
- Zulip also sends some mail From `admin_email`, and ACS rejects that unless it is a configured sender. `render` warns about it.
- **Quotas:**
  - A custom domain starts at 30 mails/minute and 100/hour; raise it with an Azure support request (Service and subscription limits) before a large realm goes live.
  - The Azure-managed domain is fixed at 5/minute and 10/hour, which only works as a stopgap.

1. `deploy-platform.zsh` creates the Email service, the domain, its sender username(s) and the Communication Services resource.
2. `acs-email.zsh --config <dept>`, run by an operator and also by `bootstrap.zsh`:
   - creates the Entra app whose client secret authenticates SMTP;
   - grants it Communication and Email Service Owner on the ACS resource;
   - stores the secret as `email-password`, with its expiry recorded for the keepalive;
   - creates the SMTP username `<prefix>-smtp`.
3. **For a custom domain:**
   - `acs-email.zsh --print` gives your DNS administrators the records: domain TXT, SPF, two DKIM CNAMEs, and a recommended DMARC.
   - Once they exist, `acs-email.zsh --verify --wait 30` verifies them and links the domain. Nothing can be sent from it before that.
4. Redeploy, then `realm.zsh --send-test-email you@princeton.edu`.

**Choosing the domain:**
- **Azure-managed** (`DoNotReply@<random>.azurecomm.net`) works with no DNS at all. It has low default sending limits and an unfamiliar sender, so it is best as a stopgap.
- **Custom domain:** Zulip also sends some mail from `admin_email`, so pick an `admin_email` on the same domain. `render` warns otherwise.
- **Rotating the secret:** `acs-email.zsh --rotate-secret`, then `update-server.zsh --restart`, then `acs-email.zsh --prune-old-secrets`.

Switching from Resend to ACS or back is a `chat.yml` change plus a redeploy. The only extra step is `acs-email.zsh` when moving to ACS.

## Per-client-IP request limits (on by default)

Zulip's nginx limits each client IP, as resolved from the proxy's `X-Forwarded-For`, before a request reaches Django:

```yaml
rate_limits:              # defaults shown
  enabled: true
  auth_per_minute: 20     # /accounts/login|register|password|find|…, /complete/, /api/v1/fetch_api_key, …
  auth_burst: 30
  api_per_second: 50      # /api/ and /json/ (includes the long-polling event queue)
  api_burst: 500
  exempt_ranges: [...]    # default: the campus ranges; never limited
```

- Over the limit, nginx answers **429**, which Zulip's clients already handle by backing off.
- The defaults are deliberately generous. Many VPN users share a few Prisma Access egress addresses, and an active client sends a request per action on top of its event poll. Tighten them for a department whose users are mostly on campus (exempt), or raise them if keepalive or users report 429s.
- The image builds the nginx config from these numbers; it never takes nginx text. Malformed values switch the limits off with a loud log line rather than break nginx.
- The e2e suite trips both limits against real Zulip nginx.

## The IP gate is optional, per server and per department

- Servers are ungated by default (`ip_gate: false`).
- `ip_gate: {enabled: false}` in `chat.yml` drops the gate for the whole department:
  - no server may turn it on;
  - no pugwips snapshot, token or refresh is needed;
  - the snapshot-age checks, the daily refresh and the snapshot PRs all stand down.
- Without the gate, sign-in is still Entra-only. [docs/edge-protection.md](docs/edge-protection.md) assesses what else stands between a public Zulip and abuse or a DDoS, and what Front Door would add.

## Monitoring with Healthchecks.io (optional)

```yaml
healthchecks:
  enabled: true
  # ping_base: https://hc-ping.com         # or a self-hosted Healthchecks
  # api_base: https://healthchecks.io/api/v3
  # health_interval_minutes: 5
```

| Check (slug) | Pinged by | Schedule |
|---|---|---|
| `<prefix>-<server>-health` | the server's `<app>-hc` scheduled job: `GET /health` from inside the environment. It works with the IP gate on and before DNS exists, and exercises nginx, Django, PostgreSQL and the sidecars | every 5 min |
| `<prefix>-<server>-web` | the config repo's daily Keepalive, run from GitHub: realms answer, Entra sign-in redirects, `/<slug>` redirects, TLS and Entra-secret expiry | daily |
| `<prefix>-<server>-ip-gate` | the daily IP gate refresh (gated servers only) | daily |
| `<prefix>-updates` | the config repo's weekly Update check | weekly |

- Every ping uses the project **ping key** plus the slug (`https://hc-ping.com/<key>/<slug>`), so there is no per-check URL to store.
  - The ping key lives in Key Vault as `healthchecks-ping-key`, which the `-hc` job reads.
  - It is also the GitHub secret `HEALTHCHECKS_PING_KEY`, which CI uses.
- With the project **API key** (`healthchecks-api-key` in Key Vault, or the GitHub secret `HEALTHCHECKS_API_KEY`), `healthchecks.zsh --sync` creates or updates every check with the schedules above. Deploys run it automatically.
- Without the API key, checks are created by their first ping with Healthchecks' default schedule, which you then adjust in the UI.
- A failure pings `/fail` with the reason, so the alert is immediate.
- A ping that cannot be sent is a workflow warning, never a silent skip.

## Keepalive and update checks

| Repo | Workflow | Schedule | What it does |
|---|---|---|---|
| config | **Keepalive** | daily | `keepalive.zsh` for every server: smoke test; TLS certificates (fail under 14 days); the Entra client secret, whose expiry `entra-app.zsh` records on its Key Vault secret (fail under 21 days); with ACS, the SMTP secret too; the pugwips snapshot age on gated servers; then pings `-web`. Also re-enables the repo's scheduled workflows, so GitHub's 60-day inactivity rule never switches them off |
| config | **Update check** | weekly | Up to two PRs. `auto/template`: a newer pu-shd/chat release. `template.lock` and every `uses:` are pinned to its commit (refusing a moved tag), `generated/` is re-rendered by the new template, and the config tests run. `auto/pugwips` (only with the gate on and `PUGWIPS_READ_TOKEN`): the static snapshot refreshed from the signed pugwips release |
| template | **Update check** | weekly | `tools/updates.py zulip`: the Zulip image (new release or upstream rebuild, tag plus digest); sidecar images (newest in the *same* major; new majors are only reported); the bicep CLI; regenerates the reserved-name list for a new Zulip version. Runs the test suite, then opens `auto/updates` |
| template | **Keepalive** | weekly | re-enables its scheduled workflows |
| template | Dependabot | weekly | GitHub Actions, pip, the test image's base |

- An update check that cannot reach a registry fails the run; it never reports "no updates".
- PRs opened with the default `GITHUB_TOKEN` do not trigger CI, and cannot change workflow files.
  - Add a fine-grained token as `UPDATE_PR_TOKEN`: Contents and Pull requests: write in the template repo, and **also Workflows: write** in config repos.
  - A config repo's template PR fails clearly without it, because it changes the pinned `uses:` lines.
- **The upgrade path:**
  1. Merge the template PR.
  2. Tag a release. It checks that the image builds and publishes a `template.lock` asset.
  3. Each department's Update check opens its PR.
  4. Merging that builds the new image into the department registry and redeploys, with the maintenance stop described under Day 2.

## Teardown

| Where | How |
|---|---|
| Locally | `scripts/teardown.zsh --config <dept> [--server x] [--purge] [--healthchecks] [--entra] [--platform] [--github]`, the reverse of `bootstrap.zsh`. It asks for one confirmation, `TEARDOWN <dept>` (or `TEARDOWN <dept> PURGE`) |
| CI | the config repo's **Teardown** workflow (manual): dept, one server or all, purge, platform, and the same phrase |

| Mode | Effect |
|---|---|
| preserve (default) | Deletes apps and jobs. Databases, uploads, secrets, identities and the hostname bindings (saved to Key Vault) stay, so the Deploy workflow (or `bootstrap.zsh --from servers`) restores the servers with their hostnames |
| `--purge` | Also destroys databases, uploads, per-server identities and secrets. Deleted secrets stay recoverable for 90 days (purge protection); redeploying the same server name recovers them |
| `--platform` | Deletes the resource group. Needs `--purge` and every server |
| `--entra`, `--github` | Delete the Zulip sign-in app registrations and CI's own access. Operator only, because CI has no Entra rights |

The single-server scripts (`teardown-server.zsh`, `teardown-platform.zsh`) remain available and have their own confirmations.

## Day 2

| Task | Command (or the config repo's **Operate** workflow) |
|---|---|
| Upgrade Zulip | Merge the template's update PR (it bumps `ZULIP_IMAGE`), tag a release, merge the config repo's update PR. Its deploy builds the new image into the registry, stops the old revision so only one Zulip migrates, and records a PostgreSQL restore point |
| Refresh IP gate (gated servers) | Daily `ip-gate.yml`, or `ip-gate.zsh --apply` |
| Check mail | `realm.zsh --send-test-email you@princeton.edu` |
| Hand a realm over / recover it | `realm.zsh --set-role <slug\|_root> <email> owner "Full Name"` (see Accounts and roles) |
| Mobile push | Apply for Zulip's free Community plan for each organization, then `realm.zsh --register-push` and set `push_notifications: true` |
| Rotate the Entra secret | `entra-app.zsh --rotate-secret`, then `update-server.zsh --restart`, then `entra-app.zsh --prune-old-secrets`; until then the old secret still works |
| Rotate the ACS SMTP secret | `acs-email.zsh --rotate-secret`, restart the servers, then `acs-email.zsh --prune-old-secrets` |
| Rebuild the image | `build-image.zsh --config <dept>`: a no-op when `chat:<ref>-<sha7>` already exists; a new release gets a new tag |
| Health right now | `keepalive.zsh --config <dept>`; with Healthchecks, `healthchecks.zsh --list` |
| Remove servers or everything | `teardown.zsh`, or the Teardown workflow (see Teardown above) |

## Custom look (themes)

`theme: paper-tiger` on a server (or per realm on a shared server; `default` keeps Zulip's look) recolours Zulip with Princeton orange. It covers buttons, links, focus, mentions, pills, the unread marker and the login pages, in light and dark.
- Themes override only Zulip's CSS variables, and the e2e suite checks each one still exists in the running Zulip.
- Logos and icons are set per realm in Zulip itself.

See [docs/theming.md](docs/theming.md).

## Tests

| Command | What runs |
|---|---|
| `docker-compose -f tests/docker-compose.yml run --rm tests` | Unit and integration tests. The renderer, the update checker and every zsh script (against recording `az`/`gh`/`dig`/`curl`/`cosign` stand-ins that fail on any unexpected call), the Bicep templates, the image entrypoint (redirects, request limits), and `chat-dbinit` against a real PostgreSQL 17 whose admin, like Azure's, is not a superuser |
| `tests/e2e/run.zsh` | Builds the image and boots Zulip with settings rendered from a real `chat.yml`, using the same sidecar commands as Bicep plus a mock Entra, a mail sink and a mock Healthchecks. It then checks health and proxy trust, the redirects, realm creation through the mgmt job, a complete OIDC sign-in, outgoing mail, the per-IP request limits (429 per client, exempt ranges untouched), and the `-hc` probe's success and failure pings |

CI (`.github/workflows/ci.yml`) runs both, plus actionlint. The Release workflow (`workflow_dispatch` for a dry run) checks that the image builds.

## Not yet verified on Azure

These depend on Azure behaviour that the local tests cannot reproduce. Check them in a scratch resource group before the first real deployment:
- PostgreSQL Flexible Server accepts `LC_COLLATE 'C.UTF-8'`. Tested on PostgreSQL 17 locally; `chat-dbinit` fails loudly if Azure disagrees.
- docker-zulip's `chown` of `/data/uploads` on the NFS share (`NoRootSquash`).
- `LOADBALANCER_IPS` set to the environment subnet: the ingress proxy's `X-Forwarded-For` must be trusted, and public clients must be denied `/health`.
- The `-mgmt` job reaching the sidecars through `additionalPortMappings` while IP restrictions are on. The gate adds an `aca-internal` allow rule for the subnet.
- `az containerapp job logs show` output, which `run_job` parses. It falls back to Log Analytics.
- The `-hc` job reaching `http://<app>:8080/health` through the internal-only TCP port mapping (plain HTTP, no redirect).
- `az containerapp job start --env-vars` merging with (not replacing) the job's environment. The purge teardown relies on it and checks the job's own `dropped` report.
- Healthchecks' `GET /api/v3/checks/?slug=` filter, which `--sync` uses to find existing checks.
- `az acr build` / `az acr import` into the Basic registry from CI (Contributor on the resource group), and Container Apps pulling with the user-assigned identities' AcrPull.
