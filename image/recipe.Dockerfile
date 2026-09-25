# The software factory's coding clients, appended to kinby's base image.
# The hub records this recipe with the instance's package selection and adds the pinned
# `uv pip install` of this distribution after it. CI builds the same image.
ARG GH_VERSION=2.82.1
ARG CLAUDE_CODE_VERSION=2.1.280
ARG CODEX_VERSION=0.154.0
ARG BUN_VERSION=1.4.2
RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates curl nodejs npm unzip \
    && rm -rf /var/lib/apt/lists/* \
    && arch="$(dpkg --print-architecture)" \
    && curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz" \
        | tar -xz -C /usr/local --strip-components=1 "gh_${GH_VERSION}_linux_${arch}/bin/gh" \
    && case "$arch" in amd64) bun_arch=x64 ;; arm64) bun_arch=aarch64 ;; *) exit 1 ;; esac \
    && curl -fsSL -o /tmp/bun.zip \
        "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/bun-linux-${bun_arch}.zip" \
    && unzip -j /tmp/bun.zip "bun-linux-${bun_arch}/bun" -d /usr/local/bin \
    && rm /tmp/bun.zip \
    && npm install --global "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force
# Claude Code must not update itself inside a pinned image. Bun has no background updater;
# only `bun upgrade` replaces it, and nothing in the factory runs that.
ENV DISABLE_AUTOUPDATER=1
