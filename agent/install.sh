#!/bin/sh
# Install FilmCraft, EffectCraft and SoundCraft CLIs plus the fcedit agent tool.

set -e

REPO="${FC_REPO:-jokedud/filmcraft-agent}"
DEST="${FC_DEST:-$HOME/.local/bin}"
APPS="${FC_APPS:-film,effect,sound}"
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)
case "$ARCH" in
  x86_64|amd64) ARCH=x86_64 ;;
  arm64|aarch64) ARCH=aarch64 ;;
  *) echo "fcedit: unsupported arch $ARCH" >&2; exit 1 ;;
esac
case "$OS" in
  linux|darwin) ;;
  *) echo "fcedit: unsupported OS $OS (on Windows use install.ps1)" >&2; exit 1 ;;
esac

need() { command -v "$1" >/dev/null 2>&1 || { echo "fcedit: need $1 installed" >&2; exit 1; }; }
need curl
need python3

api() { curl -fsSL "https://api.github.com/repos/$1/releases/latest"; }
dl() {
  echo "  downloading $1" >&2
  curl -fSL --retry 3 -o "$2" "$1"
}

mkdir -p "$DEST"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

install_app() {
  app="$1"
  case "$app" in
    film) upstream=storytold/filmcraft ;;
    effect) upstream=storytold/effectcraft ;;
    sound) upstream=storytold/soundcraft ;;
    *) echo "fcedit: unknown app '$app' in FC_APPS" >&2; exit 1 ;;
  esac
  cli="${app}craft-cli"
  target="${OS}-${ARCH}"
  case "$OS" in
    darwin) target="macos-${ARCH}" ;;
  esac
  asset_url=""
  rel=""
  if rel=$(api "$REPO" 2>/dev/null); then
    asset_url=$(printf '%s' "$rel" | grep -o '"browser_download_url": *"[^"]*"' | cut -d'"' -f4 \
      | grep "${cli}-.*-${target}\." | grep -v '\.sha256\|\.zsync' | head -1 || true)
  fi
  if [ -z "$asset_url" ] && [ "$OS" = "darwin" ]; then
    asset_url=$(printf '%s' "$rel" | grep -o '"browser_download_url": *"[^"]*"' | cut -d'"' -f4 \
      | grep "${cli}-.*-macos-universal\.zip" | grep -v '\.sha256\|\.zsync' | head -1 || true)
  fi
  if [ -z "$asset_url" ]; then
    rel=$(api "$upstream")
    if [ "$OS" = "darwin" ]; then
      pattern="${cli}-.*-macos-universal\.zip"
    elif [ "$app" = "film" ]; then
      pattern="filmcraft-.*-${OS}-${ARCH}\.tar\.gz"
    else
      pattern="${app}craft-.*-${OS}-${ARCH}\.tar\.gz"
    fi
    asset_url=$(printf '%s' "$rel" | grep -o '"browser_download_url": *"[^"]*"' | cut -d'"' -f4 \
      | grep "$pattern" | head -1 || true)
  fi
  [ -n "$asset_url" ] || { echo "fcedit: no $app release asset for ${OS}/${ARCH}" >&2; exit 1; }
  echo "fcedit: installing $app from $(basename "$asset_url")" >&2
  package="$TMP/$app.pkg"
  dl "$asset_url" "$package"
  output="$DEST/$cli"
  case "$asset_url" in
    *.zip)
      need unzip
      member=$(unzip -Z1 "$package" | grep -E "(^|/)${cli}$" | head -1 || true)
      [ -n "$member" ] || { echo "fcedit: $cli not found in archive" >&2; exit 1; }
      unzip -p "$package" "$member" > "$output"
      ;;
    *)
      member=$(tar -tzf "$package" | grep -E "(^|/)${cli}$" | head -1 || true)
      [ -n "$member" ] || { echo "fcedit: $cli not found in archive" >&2; exit 1; }
      tar -xOf "$package" "$member" > "$output"
      ;;
  esac
  chmod +x "$output"
}

old_ifs=$IFS
IFS=,
for app in $APPS; do
  IFS=$old_ifs
  install_app "$app"
  IFS=,
done
IFS=$old_ifs

dl "https://raw.githubusercontent.com/$REPO/main/agent/fcedit.py" "$DEST/fcedit"
chmod +x "$DEST/fcedit"
printf '#!/bin/sh\nexec "%s/fcedit" serve "$@"\n' "$DEST" > "$DEST/fcserve"
printf '#!/bin/sh\nexec "%s/fcedit" --app effect "$@"\n' "$DEST" > "$DEST/ecedit"
printf '#!/bin/sh\nexec "%s/fcedit" --app sound "$@"\n' "$DEST" > "$DEST/scedit"
chmod +x "$DEST/fcserve" "$DEST/ecedit" "$DEST/scedit"

echo "Installed apps: $APPS" >&2
echo "Agent tools: fcedit, fcserve, ecedit, scedit in $DEST" >&2
case ":$PATH:" in
  *":$DEST:"*) ;;
  *)
    echo "Add to PATH: export PATH=\"$DEST:\$PATH\"" >&2
    for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
      [ -f "$rc" ] && { grep -q "$DEST" "$rc" 2>/dev/null || echo "export PATH=\"$DEST:\$PATH\"" >> "$rc"; }
    done
    ;;
esac
echo "Try: fcedit --app effect tools" >&2
