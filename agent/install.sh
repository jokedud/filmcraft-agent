#!/bin/sh
# Install filmcraft-cli + fcedit for Linux and macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/jokedud/filmcraft-agent/main/agent/install.sh | sh
#
# Downloads the latest release binary for this OS/arch (this repo's slim
# filmcraft-cli packages first, upstream storytold/filmcraft releases as a
# fallback), installs fcedit alongside, and puts both on PATH.

set -e

REPO="${FC_REPO:-jokedud/filmcraft-agent}"
UPSTREAM="storytold/filmcraft"
DEST="${FC_DEST:-$HOME/.local/bin}"

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
  url="$1"; out="$2"
  echo "  downloading $url" >&2
  curl -fSL --retry 3 -o "$out" "$url"
}

mkdir -p "$DEST"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

ASSET_URL=""
VERSION=""
# 1) this repo's slim cli-only package
if REL=$(api "$REPO" 2>/dev/null); then
  VERSION=$(printf '%s' "$REL" | grep -o '"tag_name": *"[^"]*"' | head -1 | cut -d'"' -f4)
  # filmcraft-cli-<ver>-linux-x86_64.tar.gz / -macos-universal.tar.gz
  case "$OS" in
    darwin) PATTERN="filmcraft-cli-.*-macos-universal\|filmcraft-cli-.*-macos-${ARCH}" ;;
    *)      PATTERN="filmcraft-cli-.*-${OS}-${ARCH}" ;;
  esac
  ASSET_URL=$(printf '%s' "$REL" | grep -o '"browser_download_url": *"[^"]*"' | cut -d'"' -f4 \
              | grep "$PATTERN" | grep -v '\.sha256\|\.zsync' | head -1)
fi

# 2) upstream release tarball (contains the desktop app + filmcraft-cli)
if [ -z "$ASSET_URL" ]; then
  REL=$(api "$UPSTREAM")
  VERSION=$(printf '%s' "$REL" | grep -o '"tag_name": *"[^"]*"' | head -1 | cut -d'"' -f4)
  case "$OS" in
    darwin) PATTERN="filmcraft-cli-.*-macos-universal\.zip" ;;
    *)      PATTERN="filmcraft-.*-${OS}-${ARCH}\.tar\.gz" ;;
  esac
  ASSET_URL=$(printf '%s' "$REL" | grep -o '"browser_download_url": *"[^"]*"' | cut -d'"' -f4 \
              | grep "$PATTERN" | head -1)
fi

[ -n "$ASSET_URL" ] || { echo "fcedit: no release asset for ${OS}/${ARCH}" >&2; exit 1; }
echo "fcedit: installing filmcraft ${VERSION:-latest} for ${OS}/${ARCH}" >&2

case "$ASSET_URL" in
  *.zip)
    dl "$ASSET_URL" "$TMP/pkg.zip"
    (cd "$TMP" && unzip -o -q pkg.zip)
    ;;
  *)
    dl "$ASSET_URL" "$TMP/pkg.tar.gz"
    (cd "$TMP" && tar xzf pkg.tar.gz)
    ;;
esac

CLI=$(find "$TMP" -name 'filmcraft-cli' -type f | head -1)
[ -n "$CLI" ] || { echo "fcedit: filmcraft-cli not found in package" >&2; exit 1; }
cp "$CLI" "$DEST/filmcraft-cli"
chmod +x "$DEST/filmcraft-cli"

# fcedit agent tool (from this repo)
dl "https://raw.githubusercontent.com/$REPO/main/agent/fcedit.py" "$DEST/fcedit"
chmod +x "$DEST/fcedit"
ln -sf "$DEST/fcedit" "$DEST/fcserve" 2>/dev/null || true

echo "" >&2
echo "Installed:" >&2
"$DEST/filmcraft-cli" --version >&2 || true
python3 "$DEST/fcedit" --help >/dev/null 2>&1 && echo "  fcedit (agent tool) -> $DEST/fcedit" >&2

case ":$PATH:" in
  *":$DEST:"*) ;;
  *)
    echo "" >&2
    echo "Add to PATH:" >&2
    echo "  export PATH=\"$DEST:\$PATH\"" >&2
    for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
      [ -f "$rc" ] && { grep -q "$DEST" "$rc" 2>/dev/null || echo "export PATH=\"$DEST:\$PATH\"" >> "$rc"; }
    done
    ;;
esac

command -v ffmpeg >/dev/null 2>&1 || \
  echo "note: ffmpeg is optional (test oracles only); FilmCraft decodes/encodes natively." >&2
echo "fcedit: done. Try: fcedit build examples/intro-recipe.json" >&2
