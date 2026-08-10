FROM python:3.13-slim

ARG KUBECTL_VERSION=v1.36.0
ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN case "$TARGETARCH" in amd64|arm64) ;; *) echo "Unsupported kubectl architecture: $TARGETARCH" >&2; exit 1 ;; esac \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
        --output /tmp/kubectl \
    && curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl.sha256" \
        --output /tmp/kubectl.sha256 \
    && cd /tmp \
    && echo "$(cat kubectl.sha256)  kubectl" | sha256sum --check \
    && install -o root -g root -m 0755 /tmp/kubectl /usr/local/bin/kubectl \
    && rm -f /tmp/kubectl /tmp/kubectl.sha256 \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY kcp ./kcp
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/kcp-entrypoint

EXPOSE 8443
ENTRYPOINT ["/usr/local/bin/kcp-entrypoint"]
