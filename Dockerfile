# syntax=docker/dockerfile:1@sha256:572cebbc2b15e6f84c077742952674889cb63cd5e9b80abdffcbe587b6f19183
FROM python:3.15-slim-bookworm@sha256:948cabe5043f2d650d227a1b56bdba562f0224713f17e6e79b4b645ba154f941

# App version, stamped from the release tag by CI (docker-publish.yml build-args).
ARG VERSION=0.0.0-dev
ENV APP_VERSION=${VERSION} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CURATOR_DATA=/data \
    HOME=/tmp

LABEL org.opencontainers.image.source="https://github.com/LukeEvansTech/curator" \
      org.opencontainers.image.title="curator" \
      org.opencontainers.image.description="Review pages for the weekly Radarr and Sonarr cleanup" \
      org.opencontainers.image.version="${VERSION}"

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY curator ./curator

RUN mkdir -p /data && chown -R 65532:65532 /data
VOLUME ["/data"]

USER 65532:65532
# Kubernetes probes own liveness (/health); an image healthcheck would double-probe.
HEALTHCHECK NONE
EXPOSE 8080
ENTRYPOINT ["python", "-m", "curator"]
CMD ["serve"]
