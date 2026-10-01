# Edge protection without the IP gate

What protects a public Zulip server here once `ip_gate` is off, and whether Azure Front Door (or anything else) is worth adding. Facts about Azure were checked against Microsoft Learn and Azure list prices on 2026-09-30; prices are East US / North America list prices.

## What is already in place (no extra cost)

| Layer | Protection | Covers |
|---|---|---|
| Azure platform | Always-on infrastructure DDoS protection for Azure public endpoints, including the Container Apps environment's inbound IP | Large L3/L4 floods, on a best-effort basis that is not tuned to this app |
| Sign-in | Entra ID OIDC only. There is no password backend (`EmailAuthBackend` is off, so `fetch_api_key` with a password is off). Tenant-restricted issuer, assignment required on the enterprise app, Entra's own lockout, MFA and Conditional Access | Credential stuffing and password spraying, which have nowhere to land. Sign-up on shared servers is by invitation |
| Zulip | Per-user and per-IP rate limits on API and auth endpoints. They see the real client IP, because nginx trusts `X-Forwarded-For` only from the Container Apps subnet (`LOADBALANCER_IPS`) | Abuse by one client: scraping, brute force against remaining endpoints |
| Surface | Only 443 is exposed. `/health` is refused to public clients. Redis, RabbitMQ and memcached are internal-only and password-protected. PostgreSQL has no public endpoint | Port scanning, and direct service attacks |
| Detection | Healthchecks (`-health` every 5 min, `-web` daily), the keepalive, and Log Analytics | Knowing within minutes that a server is down |

**The weak point is capacity, not access control.** Each Zulip server is exactly one replica: Tornado cannot scale out behind a load balancer without sharding. A modest application-layer flood of expensive requests can therefore exhaust one server's 2 vCPU. The two defences are:
- stopping bad traffic before it reaches the replica;
- keeping the blast radius to one server, which the dedicated/shared split already does.

## Options

| Option | Adds | Approximate cost | Fit |
|---|---|---|---|
| **A. nginx rate limits**: **implemented, on by default** (`rate_limits` in `chat.yml`) | Per-client-IP request budgets on sign-in, sign-up, API-key and API paths, in front of Django. Campus ranges exempt | $0 | Good first step. It stops a single-source flood, not a distributed one |
| **B. Front Door Standard + WAF custom rules** | Global anycast edge, with L3/4/7 DDoS absorbed at the edge at no extra cost. WAF **custom rules and rate-limit rules**, geo filtering, caching of Zulip's hashed `/static/` assets, TLS at the edge | $35/month base, plus about $0.009 per 10k requests and about $0.083/GB egress | **Recommended if the servers are ever targeted**, or as the default if public exposure worries you |
| **C. Front Door Premium** | B, plus Microsoft-managed rule sets (DRS), bot manager, and **Private Link to the Container Apps environment**, which lets the app's public ingress be switched off | $330/month base, plus requests, egress and the private endpoint | For a department that needs managed OWASP rules or bot defence, or wants no public origin at all |
| D. Application Gateway WAF v2 | Regional WAF (CRS 3.2 / DRS, bodies to 2 MB, files to 4 GB) | About $300–400/month | Needs an **internal** Container Apps environment, which cannot be switched from external: the platform would have to be rebuilt. Not recommended here |
| E. DDoS Network Protection / IP Protection | Tuned L3/4 mitigation, cost protection, rapid response | About $2,944/month (Network, up to 100 IPs) or $199/month per IP | Not documented for a Container Apps environment's platform-managed IP. Front Door's edge protection covers the same need more cheaply. Not recommended |

## If Front Door is added: what must change

1. **Long polling:** set the origin response timeout to 120 s. The default is 60 s, the range 16–240 s. Zulip's event polls last up to about 55 s, and Container Apps allows 240 s.
2. **Uploads:**
   - Front Door passes bodies up to 2 GB.
   - The WAF inspects only the first 128 KB of a body (Standard and Premium). Keep body inspection on for small API calls, but don't expect it to police uploads.
   - Exclude `/api/v1/tus` and `/user_uploads` from any managed-rule body matching (Premium) to avoid false positives.
3. **Lock the origin to Front Door**, or attackers simply go around it:
   - **Standard:** Container Apps IP rules do not accept service tags. Maintain the published `AzureFrontDoor.Backend` IPv4 ranges as an allowlist; the IP-gate machinery already syncs a signed, validated allowlist and could take this second source.
   - **Also on Standard:** require the `X-Azure-FDID` header with this profile's ID in nginx, because the backend ranges are shared by every Front Door customer.
   - **Premium:** use Private Link to the environment and set its public network access to Disabled. This is supported on workload-profiles environments like this one.
4. **Host names and certificates.** Keep the original Host (Microsoft's recommendation), so Zulip sees `chat.<dept>.princeton.edu`:
   - The custom domain must stay bound on the Container App with a certificate that Front Door validates.
   - Once public DNS points at Front Door, an ACA **managed** certificate can no longer be issued or renewed, so bind a **Key Vault certificate** on the app (the `cert.key_vault_certificate` path gated servers already use).
   - Front Door gets its own managed certificate for the same names.
   - The DNS records change from `CNAME → <app>.<env domain>` to `CNAME → <endpoint>.azurefd.net`, plus Front Door's own `_dnsauth` TXT validation record.
5. **Real client IPs.** Zulip must trust `X-Forwarded-For` from Front Door's ranges as well as from the environment subnet, or its per-IP rate limits will see Front Door instead of users.

## Recommendation

- **Now (free), done:** drop the IP gate where it gets in the way (`ip_gate.enabled: false`) and rely on **Option A**, the per-client-IP `limit_req` budgets now built into the image (`rate_limits`). Together with Entra-only sign-in, Zulip's own limits and Azure's platform DDoS protection, that is proportionate for departmental chat.
- **If a server is targeted, or before exposing a high-profile one:** add **Option B**, Front Door Standard with WAF rate-limit rules and origin lock by backend-range allowlist plus `X-Azure-FDID`. It is the cheapest way to move volumetric and L7 floods off the single replica, at about $35/month plus traffic for the whole department. One profile can front every server.
- **Premium (Option C)** only if managed rule sets, bot manager or a fully private origin become requirements.

## Not verified

These are gaps in the public documentation:
- Whether DDoS IP Protection can cover a Container Apps environment IP.
- How the Front Door WAF treats bodies over 128 KB. Community answers say only the first 128 KB is inspected, but the official page doesn't confirm it.
