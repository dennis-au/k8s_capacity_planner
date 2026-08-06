FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN python -m venv /opt/venv \
    && groupadd --gid 10001 kcp \
    && useradd --uid 10001 --gid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin kcp

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=kcp:kcp kcp ./kcp
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/kcp-entrypoint

USER 10001:10001
EXPOSE 8443
ENTRYPOINT ["/usr/local/bin/kcp-entrypoint"]
