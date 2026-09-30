# Test image for pu-shd/chat: everything the unit + integration suites need, so CI and
# a laptop run the same thing:  docker-compose -f tests/docker-compose.yml run --rm tests
FROM python:3.14-slim

# zsh/jq/openssl: the operations scripts. bash: the image entrypoint scripts.
# postgresql-client: chat-dbinit integration tests. dnsutils/curl exist but are shimmed.
RUN apt-get update \
 && apt-get install -y --no-install-recommends zsh jq curl openssl bash dnsutils \
      postgresql-client ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ARG BICEP_VERSION=v0.43.8
ARG TARGETARCH
# bicep is a .NET single-file binary; without ICU it aborts unless globalization is invariant.
ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
RUN arch="$( [ "${TARGETARCH:-amd64}" = arm64 ] && echo arm64 || echo x64 )" \
 && curl -fsSLo /usr/local/bin/bicep \
      "https://github.com/Azure/bicep/releases/download/${BICEP_VERSION}/bicep-linux-${arch}" \
 && chmod +x /usr/local/bin/bicep \
 && bicep --version

WORKDIR /src
COPY tools/requirements.txt tools/requirements.txt
COPY tests/requirements.txt tests/requirements.txt
RUN python -m venv /venv && /venv/bin/pip install --no-cache-dir -r tests/requirements.txt
ENV PATH=/venv/bin:$PATH \
    CHAT_PY=/venv/bin/python \
    CHAT_REQUIRE_BICEP=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY . /src
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/unit", "tests/integration"]
