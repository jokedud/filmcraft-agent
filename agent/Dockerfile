# Multi-Craft headless engines + agent HTTP service.
#
#   docker build -f agent/Dockerfile -t fcedit .
#   docker run -p 8321:8321 -e FCEDIT_TOKEN=replace-me \
#     -v "$PWD/media:/media" fcedit
# The service binds 0.0.0.0 in the container. Set FCEDIT_TOKEN or explicitly
# pass --insecure-no-auth only on a trusted network. Mount media under /media.

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 curl ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/fcedit

ARG FC_REPO=jokedud/filmcraft-agent
RUN set -e; \
    for app in film effect sound; do \
      cli="${app}craft-cli"; \
      case "$app" in \
        film) upstream=storytold/filmcraft ;; \
        effect) upstream=storytold/effectcraft ;; \
        sound) upstream=storytold/soundcraft ;; \
      esac; \
      rel=$(curl -fsSL "https://api.github.com/repos/$FC_REPO/releases/latest" 2>/dev/null || true); \
      url=""; \
      if [ -n "$rel" ]; then \
        url=$(printf '%s' "$rel" | grep -o '"browser_download_url": *"[^"]*"' | cut -d'"' -f4 \
          | grep "${cli}-.*-linux-x86_64\.tar\.gz" | head -1 || true); \
      fi; \
      if [ -z "$url" ]; then \
        rel=$(curl -fsSL "https://api.github.com/repos/$upstream/releases/latest"); \
        url=$(printf '%s' "$rel" | grep -o '"browser_download_url": *"[^"]*"' | cut -d'"' -f4 \
          | grep "${app}craft-.*-linux-x86_64\.tar\.gz" | head -1 || true); \
      fi; \
      test -n "$url"; \
      curl -fSL -o /tmp/pkg "$url"; \
      member=$(tar -tzf /tmp/pkg | grep -E "(^|/)${cli}$" | head -1); \
      tar -xOzf /tmp/pkg "$member" > "/usr/local/bin/$cli"; \
      chmod +x "/usr/local/bin/$cli"; \
    done

COPY agent/fcedit.py /usr/local/bin/fcedit
RUN chmod +x /usr/local/bin/fcedit \
    && printf '#!/bin/sh\nexec /usr/local/bin/fcedit --app effect "$@"\n' > /usr/local/bin/ecedit \
    && printf '#!/bin/sh\nexec /usr/local/bin/fcedit --app sound "$@"\n' > /usr/local/bin/scedit \
    && printf '#!/bin/sh\nexec /usr/local/bin/fcedit serve "$@"\n' > /usr/local/bin/fcserve \
    && chmod +x /usr/local/bin/ecedit /usr/local/bin/scedit /usr/local/bin/fcserve

EXPOSE 8321
VOLUME /media
ENTRYPOINT ["python3", "/usr/local/bin/fcedit"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8321"]
