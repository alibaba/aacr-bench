#!/bin/sh
set -eu

architecture="${TARGETARCH:-$(dpkg --print-architecture)}"
case "$architecture" in
  amd64) node_arch=x64 ;;
  arm64) node_arch=arm64 ;;
  *) echo "unsupported platform: $architecture" >&2; exit 1 ;;
esac

if [ "${INSTALL_CLAUDE:-1}" = 1 ] || [ "${INSTALL_CODEX:-1}" = 1 ]; then
  node_version="${NODE_VERSION:-lts}"
  if [ "$node_version" = lts ]; then
    curl -fsSL --retry 2 --connect-timeout 30 --max-time 300 \
      "${NODE_DIST_URL:-https://nodejs.org/dist}/index.json" -o /tmp/node-index.json
    node_version="$(python3 -c 'import json; rows=json.load(open("/tmp/node-index.json")); print(next(row["version"] for row in rows if row.get("lts") and {"linux-x64", "linux-arm64"}.issubset(row.get("files", []))))')"
  fi
  node_version="${node_version#v}"
  curl -fsSL --retry 2 --connect-timeout 30 --max-time 300 \
    "${NODE_DIST_URL:-https://nodejs.org/dist}/v$node_version/node-v$node_version-linux-$node_arch.tar.xz" -o /tmp/node.tar.xz
  tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1
  node --version
  npm --version
fi

if [ "${INSTALL_OCR:-1}" = 1 ]; then
  ocr_version="${OCR_VERSION:-latest}"
  case "$ocr_version" in
    [0-9]*) ;;
    *)
    curl -fsSL --retry 2 --connect-timeout 30 --max-time 300 \
      "${NPM_REGISTRY:-https://registry.npmjs.org}/@alibaba-group%2Focr-linux-$node_arch/$ocr_version" -o /tmp/ocr-package.json
    ocr_version="$(python3 -c 'import json; print(json.load(open("/tmp/ocr-package.json"))["version"])')"
    ;;
  esac
  curl -fsSL --retry 2 --connect-timeout 30 --max-time 300 \
    "${NPM_REGISTRY:-https://registry.npmjs.org}/@alibaba-group/ocr-linux-$node_arch/-/ocr-linux-$node_arch-$ocr_version.tgz" -o /tmp/ocr.tgz
  tar -xzOf /tmp/ocr.tgz package/bin/opencodereview > /usr/local/bin/ocr
  chmod +x /usr/local/bin/ocr
  ocr version
fi

if [ "${INSTALL_CLAUDE:-1}" = 1 ]; then
  npm install --global --registry "${NPM_REGISTRY:-https://registry.npmjs.org}" "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION:-latest}"
  claude --version
fi
if [ "${INSTALL_CODEX:-1}" = 1 ]; then
  npm install --global --registry "${NPM_REGISTRY:-https://registry.npmjs.org}" "@openai/codex@${CODEX_VERSION:-latest}"
  codex --version
fi
