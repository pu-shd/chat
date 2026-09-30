# pu-shd/chat Zulip image: upstream docker-zulip plus the few hooks the Azure
# vending needs. Everything else is configured through docker-zulip's own
# SETTING_* / CONFIG_* / SECRETS_* environment and /run/secrets/zulip__* files.
#
# The base is pinned by digest; bump it (and the tag) together when upgrading Zulip.
ARG ZULIP_IMAGE=ghcr.io/zulip/zulip-server:12.3-0@sha256:76a1ecc4ba5470869d9609ef1ec16497b410189918b2daba85862183a59aa275
FROM ${ZULIP_IMAGE}

LABEL org.opencontainers.image.source="https://github.com/pu-shd/chat" \
      org.opencontainers.image.description="Zulip for Azure Container Apps, vended by pu-shd/chat" \
      org.opencontainers.image.licenses="MIT"

COPY bin/ /usr/local/bin/
RUN chmod 0755 /usr/local/bin/chat-*

ENTRYPOINT ["/usr/local/bin/chat-entrypoint"]
CMD ["app:run"]
