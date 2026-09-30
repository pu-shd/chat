# pu-shd/chat Zulip image: upstream docker-zulip plus the few hooks the Azure
# vending needs. Everything else is configured through docker-zulip's own
# SETTING_* / CONFIG_* / SECRETS_* environment and /run/secrets/zulip__* files.
# Each department builds it into its own registry from the pinned commit
# (scripts/build-image.zsh, az acr build).
#
# The base is pinned by digest; bump it (and the tag) together when upgrading Zulip.
ARG ZULIP_IMAGE=ghcr.io/zulip/zulip-server:12.3-0@sha256:76a1ecc4ba5470869d9609ef1ec16497b410189918b2daba85862183a59aa275
FROM ${ZULIP_IMAGE}
# The pu-shd/chat commit this image was built from (build-image.zsh passes it).
ARG CHAT_TEMPLATE_SHA=unknown

LABEL org.opencontainers.image.source="https://github.com/pu-shd/chat" \
      org.opencontainers.image.revision="${CHAT_TEMPLATE_SHA}" \
      org.opencontainers.image.description="Zulip for Azure Container Apps, vended by pu-shd/chat" \
      org.opencontainers.image.licenses="MIT"

COPY bin/ /usr/local/bin/
# Themes a chat.yml can select; served at /chat-theme/<name>/ (see chat-entrypoint).
COPY themes/ /usr/local/share/chat-themes/
RUN chmod 0755 /usr/local/bin/chat-* && rm -f /usr/local/share/chat-themes/README.md

ENTRYPOINT ["/usr/local/bin/chat-entrypoint"]
CMD ["app:run"]
