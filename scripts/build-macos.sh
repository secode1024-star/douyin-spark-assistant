#!/usr/bin/env bash
# Build on macOS, separately on Apple Silicon and Intel.
# Default output is a development build, without Developer ID notarization.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "macOS installers must be built on macOS." >&2
    exit 1
fi

SKIP_INSTALL=0
if [[ $# -gt 0 ]]; then
    if [[ $# -ne 1 || "$1" != "--skip-install" ]]; then
        echo "Usage: bash scripts/build-macos.sh [--skip-install]" >&2
        exit 2
    fi
    SKIP_INSTALL=1
fi

PYTHON="$ROOT_DIR/.venv/bin/python"
if [[ "$SKIP_INSTALL" -eq 0 ]]; then
    if [[ ! -x "$PYTHON" ]]; then
        python3 -m venv "$ROOT_DIR/.venv"
    fi
    "$PYTHON" -m pip install -r requirements-build.txt
elif [[ ! -x "$PYTHON" ]]; then
    echo "--skip-install requires an existing .venv with requirements-build.txt installed." >&2
    exit 1
fi

MACHINE="$("$PYTHON" -c 'import platform; print(platform.machine().lower())')"
case "$MACHINE" in
    arm64|aarch64) ARCH="arm64" ;;
    x86_64|amd64) ARCH="x64" ;;
    *) echo "Unsupported Python architecture: $MACHINE" >&2; exit 1 ;;
esac
VERSION="$("$PYTHON" -c 'from spark_assistant import __version__; print(__version__)')"
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?$ ]]; then
    echo "Invalid application version: $VERSION" >&2
    exit 1
fi

# This directory contains browser binaries only, never a user's browser profile.
export PLAYWRIGHT_BROWSERS_PATH="$ROOT_DIR/.local/browsers"
"$PYTHON" -m playwright install chromium
"$PYTHON" scripts/make_icons.py
"$PYTHON" scripts/collect_licenses.py
"$PYTHON" scripts/write_build_info.py
"$PYTHON" -m PyInstaller --clean --noconfirm packaging/SparkAssistant.spec

APP_PATH="$ROOT_DIR/dist/SparkAssistant.app"
if [[ ! -d "$APP_PATH" ]]; then
    echo "PyInstaller did not produce dist/SparkAssistant.app." >&2
    exit 1
fi

# Preserve each browser's complete bundle, symlinks, binary layout and signatures.
# Passing this tree to PyInstaller Analysis would trigger per-Mach-O rewriting.
BUNDLED_BROWSERS="$APP_PATH/Contents/Resources/browsers"
if [[ -L "$BUNDLED_BROWSERS" ]]; then
    echo "Unexpected browser destination symlink; use a clean build directory." >&2
    exit 1
fi
if [[ -d "$BUNDLED_BROWSERS" ]]; then
    # This fixed target is generated application content, not a user data directory.
    rm -rf -- "$BUNDLED_BROWSERS"
fi
mkdir -p "$BUNDLED_BROWSERS"
COPIED_CHROMIUM=0
for SOURCE in "$PLAYWRIGHT_BROWSERS_PATH"/chromium-* \
              "$PLAYWRIGHT_BROWSERS_PATH"/ffmpeg-* \
              "$PLAYWRIGHT_BROWSERS_PATH"/winldd-*; do
    [[ -d "$SOURCE" ]] || continue
    /usr/bin/ditto "$SOURCE" "$BUNDLED_BROWSERS/$(basename "$SOURCE")"
    case "$(basename "$SOURCE")" in chromium-*) COPIED_CHROMIUM=1 ;; esac
done
if [[ "$COPIED_CHROMIUM" -ne 1 ]]; then
    echo "No Chromium bundle was copied into the application." >&2
    exit 1
fi

# Sign only the completed outer bundle, preserving vendor browser signatures.
# An optional identity applies to this development bundle only; this is not a
# complete hardened-runtime/notarization pipeline for public distribution.
/usr/bin/codesign --force --sign "${SPARK_CODESIGN_IDENTITY:--}" "$APP_PATH"
/usr/bin/codesign --verify --all-architectures --deep --strict "$APP_PATH"
if [[ -n "${SPARK_CODESIGN_IDENTITY:-}" ]]; then
    echo "Outer bundle signed with the supplied identity. Public distribution notarization is not implemented."
else
    echo "Building an ad-hoc signed development DMG without Developer ID or notarization."
fi

mkdir -p "$ROOT_DIR/build" "$ROOT_DIR/release"
STAGING="$(mktemp -d "$ROOT_DIR/build/dmg-${ARCH}.XXXXXX")"
cleanup() {
    # Only remove the exact temporary directory created above inside this workspace.
    case "$STAGING" in
        "$ROOT_DIR"/build/dmg-*) rm -rf -- "$STAGING" ;;
        *) echo "Refusing to remove unexpected staging directory: $STAGING" >&2 ;;
    esac
}
trap cleanup EXIT

/usr/bin/ditto "$APP_PATH" "$STAGING/SparkAssistant.app"
ln -s /Applications "$STAGING/Applications"
OUTPUT="$ROOT_DIR/release/SparkAssistant-${VERSION}-macos-${ARCH}.dmg"
/usr/bin/hdiutil create -volname "Spark Assistant" -srcfolder "$STAGING" \
    -ov -format UDZO "$OUTPUT"
(
    cd "$ROOT_DIR/release"
    /usr/bin/shasum -a 256 "$(basename "$OUTPUT")" > "$(basename "$OUTPUT").sha256"
)
echo "Created: $OUTPUT"

