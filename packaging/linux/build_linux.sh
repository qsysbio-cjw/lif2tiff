#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
VERSION="2.0.0-beta.1"
SYSTEM_PYTHON="${PYTHON_BIN:-$(command -v python3.12 || command -v python3)}"
BOOTSTRAP_PYTHON="${PIP_BOOTSTRAP_PYTHON:-$(command -v python3)}"
VENV="$SCRIPT_DIR/.venv-build"
PYTHON="$VENV/bin/python"

if [[ "${1:-}" == "--clean" ]]; then
    rm -rf "$SCRIPT_DIR/build" "$SCRIPT_DIR/dist" "$SCRIPT_DIR/release"
fi

if [[ ! -x "$PYTHON" ]]; then
    "$SYSTEM_PYTHON" -m venv --without-pip "$VENV"
fi
if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
    "$BOOTSTRAP_PYTHON" -m pip --python "$PYTHON" install pip
fi

"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install -r "$ROOT/packaging/requirements-build.txt"

"$PYTHON" -m PyInstaller --noconfirm --clean \
    --distpath "$SCRIPT_DIR/dist" --workpath "$SCRIPT_DIR/build/gui" \
    "$SCRIPT_DIR/LIF2TIFF-GUI.spec"
"$PYTHON" -m PyInstaller --noconfirm --clean \
    --distpath "$SCRIPT_DIR/dist" --workpath "$SCRIPT_DIR/build/cli" \
    "$SCRIPT_DIR/LIF2TIFF-CLI.spec"

GUI="$SCRIPT_DIR/dist/LIF2TIFF-GUI/LIF2TIFF-GUI"
CLI="$SCRIPT_DIR/dist/lif2tiff"
[[ -x "$GUI" && -x "$CLI" ]] || { echo "Missing packaged executable" >&2; exit 1; }

"$CLI" --version
"$CLI" plan "$ROOT/tests/fixtures/public/ome_pr2729/output_collision/Sample.lif" >/dev/null
QT_QPA_PLATFORM=offscreen "$GUI" --smoke-test

PACKAGE_NAME="LIF2TIFF-Linux-x86_64-v$VERSION"
PACKAGE="$SCRIPT_DIR/release/$PACKAGE_NAME"
mkdir -p "$PACKAGE/GUI"
cp -a "$SCRIPT_DIR/dist/LIF2TIFF-GUI/." "$PACKAGE/GUI/"
cp "$CLI" "$PACKAGE/lif2tiff"
cp "$ROOT/packaging/RELEASE_README.md" "$PACKAGE/README.md"
cp "$ROOT/RELEASE_NOTES.md" "$ROOT/THIRD_PARTY_NOTICES.md" "$ROOT/LICENSE" "$PACKAGE/"
cp -a "$ROOT/third_party_licenses" "$PACKAGE/"

tar -C "$SCRIPT_DIR/release" -czf "$SCRIPT_DIR/release/$PACKAGE_NAME.tar.gz" "$PACKAGE_NAME"
sha256sum "$SCRIPT_DIR/release/$PACKAGE_NAME.tar.gz" > "$SCRIPT_DIR/release/$PACKAGE_NAME.tar.gz.sha256"
printf 'Release: %s\n' "$SCRIPT_DIR/release/$PACKAGE_NAME.tar.gz"
