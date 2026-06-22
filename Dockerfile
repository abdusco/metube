FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock docker-entrypoint.sh ./

ARG TARGETARCH

# Use sed to strip carriage-return characters from the entrypoint script (in case building on Windows)
# Install dependencies
RUN sed -i 's/\r$//g' docker-entrypoint.sh && \
    chmod +x docker-entrypoint.sh && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      ffmpeg \
      unzip \
      aria2 \
      coreutils \
      gosu \
      curl \
      tini \
      build-essential && \
    UV_PROJECT_ENVIRONMENT=/usr/local uv sync --frozen --no-dev --compile-bytecode && \
    uv cache clean && \
    apt-get purge -y --auto-remove build-essential && \
    rm -rf /var/lib/apt/lists/* && \
    mkdir /.cache && chmod 777 /.cache

# Install Deno by downloading the binary directly — the official install.sh runs Deno
# after extraction (for shell-setup), which crashes under QEMU arm64 emulation (SIGILL).
RUN case "$TARGETARCH" in \
      amd64) DENO_ARCH="x86_64-unknown-linux-gnu" ;; \
      arm64) DENO_ARCH="aarch64-unknown-linux-gnu" ;; \
      *) echo "Unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac && \
    DENO_VERSION=$(curl -sf https://dl.deno.land/release-latest.txt | tr -d '[:space:]') && \
    curl -fsSL "https://dl.deno.land/release/${DENO_VERSION}/deno-${DENO_ARCH}.zip" -o /tmp/deno.zip && \
    unzip -q /tmp/deno.zip deno -d /usr/local/bin/ && \
    rm /tmp/deno.zip && \
    chmod +x /usr/local/bin/deno

RUN BGUTIL_TAG="$(curl -Ls -o /dev/null -w '%{url_effective}' https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest | sed 's#.*/tag/##')" && \
    case "$TARGETARCH" in \
      amd64) BGUTIL_ARCH="x86_64" ;; \
      arm64) BGUTIL_ARCH="aarch64" ;; \
      *) echo "Unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac && \
    curl -L -o /usr/local/bin/bgutil-pot \
      "https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/download/${BGUTIL_TAG}/bgutil-pot-linux-${BGUTIL_ARCH}" && \
    chmod +x /usr/local/bin/bgutil-pot && \
    PLUGIN_DIR="$(python3 -c 'import site; print(site.getsitepackages()[0])')" && \
    curl -L -o /tmp/bgutil-ytdlp-pot-provider-rs.zip \
      "https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/download/${BGUTIL_TAG}/bgutil-ytdlp-pot-provider-rs.zip" && \
    unzip -q /tmp/bgutil-ytdlp-pot-provider-rs.zip -d "${PLUGIN_DIR}" && \
    rm /tmp/bgutil-ytdlp-pot-provider-rs.zip

COPY app ./app
COPY ui/ ./ui/

ENV PUID=1000
ENV PGID=1000
ENV UMASK=022

ENV DOWNLOAD_DIR=/downloads
ENV STATE_DIR=/downloads/.metube
ENV TEMP_DIR=/downloads
ENV PORT=8081
VOLUME /downloads
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD curl -fsS "http://localhost:${PORT}/" || exit 1

# Add build-time argument for version
ARG VERSION=dev
ENV METUBE_VERSION=$VERSION

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "./docker-entrypoint.sh"]
