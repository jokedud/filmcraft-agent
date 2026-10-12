# FilmCraft headless engine + agent HTTP service.
#
#   docker build -f agent/Dockerfile -t fcedit .
#   docker run -p 8321:8321 -v $PWD/media:/media fcedit
#   curl -X POST localhost:8321/recipe -H 'Content-Type: application/json' -d @recipe.json
#
# Media paths inside recipes must point under /media (or whichever volume you mount).

FROM debian:bookworm-slim AS base
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 curl ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/fcedit

# filmcraft-cli release binary (linux-x86_64). Prefer this repo's slim package;
# fall back to the upstream storytold release tarball.
ARG FC_REPO=jokedud/filmcraft-agent
ARG UPSTREAM=storytold/filmcraft
RUN set -e; \
    url=""; \
    rel=$(curl -fsSL "https://api.github.com/repos/$FC_REPO/releases/latest" 2>/dev/null || true); \
    if [ -n "$rel" ]; then \
      url=$(printf '%s' "$rel" | grep -o '"browser_download_url": *"[^"]*"' | cut -d'"' -f4 \
            | grep 'filmcraft-cli-.*-linux-x86_64' | grep -v sha256 | head -1); \
    fi; \
    if [ -z "$url" ]; then \
      rel=$(curl -fsSL "https://api.github.com/repos/$UPSTREAM/releases/latest"); \
      url=$(printf '%s' "$rel" | grep -o '"browser_download_url": *"[^"]*"' | cut -d'"' -f4 \
            | grep 'filmcraft-.*-linux-x86_64\.tar\.gz' | head -1); \
    fi; \
    curl -fSL -o /tmp/pkg "$url"; \
    mkdir -p /tmp/x && tar xzf /tmp/pkg -C /tmp/x 2>/dev/null || (cd /tmp/x && python3 -c "import zipfile;zipfile.ZipFile('/tmp/pkg').extractall()"); \
    cp "$(find /tmp/x -name filmcraft-cli -type f | head -1)" /usr/local/bin/filmcraft-cli; \
    chmod +x /usr/local/bin/filmcraft-cli

COPY agent/fcedit.py /usr/local/bin/fcedit
RUN chmod +x /usr/local/bin/fcedit

EXPOSE 8321
VOLUME /media
ENTRYPOINT ["python3", "/usr/local/bin/fcedit"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8321"]
