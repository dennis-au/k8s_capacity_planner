FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN python -m venv /opt/venv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY kcp ./kcp
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/kcp-entrypoint

EXPOSE 8443
ENTRYPOINT ["/usr/local/bin/kcp-entrypoint"]
