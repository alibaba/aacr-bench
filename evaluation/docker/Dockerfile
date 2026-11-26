# syntax=docker/dockerfile:1
ARG BASE_IMAGE=python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
FROM ${BASE_IMAGE}
ARG TARGETARCH
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=
ARG APT_MIRROR=deb.debian.org
ARG AACR_FRAMEWORK_COMMIT=unknown
ARG OCR_VERSION=latest
ARG NPM_REGISTRY=https://registry.npmjs.org
ARG NODE_VERSION=lts
ARG NODE_DIST_URL=https://nodejs.org/dist
ARG INSTALL_OCR=1
ARG INSTALL_CLAUDE=1
ARG INSTALL_CODEX=1
ARG CLAUDE_CODE_VERSION=latest
ARG CODEX_VERSION=latest

LABEL org.aacr.ocr.version=${OCR_VERSION} \
      org.aacr.claude.version=${CLAUDE_CODE_VERSION} \
      org.aacr.codex.version=${CODEX_VERSION}

ENV OCR_NO_UPDATE=1 \
    DISABLE_AUTOUPDATER=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AACR_EVALUATION_DIR=/opt/aacr/evaluation \
    AACR_FRAMEWORK_COMMIT=${AACR_FRAMEWORK_COMMIT}

RUN set -eu; \
    if [ "${APT_MIRROR}" != "deb.debian.org" ]; then \
      sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends git ca-certificates curl passwd xz-utils

RUN apt-get install -y --no-install-recommends libatomic1

COPY docker/install_tools.sh /opt/aacr/bin/install_tools.sh
RUN sh /opt/aacr/bin/install_tools.sh

COPY requirements.txt /opt/aacr/evaluation/requirements.txt
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" \
    ${PIP_TRUSTED_HOST:+--trusted-host "${PIP_TRUSTED_HOST}"} \
    -r /opt/aacr/evaluation/requirements.txt

COPY . /opt/aacr/evaluation/
COPY docker/entrypoint.py /opt/aacr/bin/
RUN chmod +x /opt/aacr/bin/entrypoint.py

RUN useradd --create-home --uid 1000 aacr \
 && mkdir -p /work/pr /work/out /work/snapshots/repos /work/repo-runtime /work/results-runtime /work/home-runtime /work/codex-runtime \
 && chmod -R 0777 /work \
 && chown -R aacr:aacr /opt/aacr

USER aacr
WORKDIR /work
ENTRYPOINT ["/opt/aacr/bin/entrypoint.py"]
