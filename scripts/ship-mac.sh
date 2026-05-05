#!/usr/bin/env bash
# ship-mac.sh — build everything for macOS in one shot.
#
#   1. CMake build of step2glb
#   2. bundle_macos.sh (collect dylibs, rewrite rpaths, ad-hoc codesign)
#   3. Sync bin/ + lib/ into step_importer/
#   4. Optional: install into the live Blender addons folder for testing
#   5. Refresh Step2Blend-vX.Y-mac-arm64.zip in the repo root
#
# Usage:
#   ./scripts/ship-mac.sh           # build + zip
#   ./scripts/ship-mac.sh --install # also copy into Blender's addons dir
#   ./scripts/ship-mac.sh --clean   # wipe build/ + bundle/ first
#
# Run from anywhere; the script resolves the repo root relative to itself.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BLENDER_VERSION="${BLENDER_VERSION:-5.1}"
INSTALL=false
CLEAN=false

for arg in "$@"; do
    case "$arg" in
        --install) INSTALL=true ;;
        --clean)   CLEAN=true ;;
        -h|--help)
            sed -n '2,15p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "Unknown arg: $arg (use --help)" >&2
            exit 2
            ;;
    esac
done

# ── Read version from bl_info ────────────────────────────────────────────────
VERSION=$(python3 - <<'PY'
import re, sys
src = open("step_importer/__init__.py").read()
m = re.search(r'"version"\s*:\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)', src)
if not m:
    sys.exit("could not parse bl_info[\"version\"] from step_importer/__init__.py")
print(".".join(m.groups()))
PY
)
cd "$REPO"

ZIP_NAME="Step2Blend-v${VERSION}-mac-arm64.zip"
echo "── Step 2 Blend v${VERSION} — Mac build ─────────────────────────────"

# ── 1. CMake build ───────────────────────────────────────────────────────────
cd "$REPO/step2glb"
if $CLEAN; then
    rm -rf build bundle
fi
if [[ ! -d build ]]; then
    cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
fi
cmake --build build --parallel "$(sysctl -n hw.ncpu)"

# ── 2. Bundle dylibs ─────────────────────────────────────────────────────────
bash bundle_macos.sh

# ── 3. Sync into step_importer/ ──────────────────────────────────────────────
# Force-remove first — `cp` over a launched binary on macOS can silently
# fail to overwrite, leaving a stale binary in place.
rm -rf "$REPO/step_importer/bin" "$REPO/step_importer/lib"
cp -R bundle/bin "$REPO/step_importer/bin"
cp -R bundle/lib "$REPO/step_importer/lib"

# ── 4. Optional: install into live Blender ───────────────────────────────────
if $INSTALL; then
    BL_ADDONS="$HOME/Library/Application Support/Blender/${BLENDER_VERSION}/scripts/addons"
    if [[ -d "$BL_ADDONS" ]]; then
        rm -rf "$BL_ADDONS/step_importer"
        cp -R "$REPO/step_importer" "$BL_ADDONS/step_importer"
        echo "Installed into Blender ${BLENDER_VERSION}"
    else
        echo "WARN: Blender ${BLENDER_VERSION} addons dir not found at:"
        echo "      $BL_ADDONS"
        echo "      Set BLENDER_VERSION=X.Y to override."
    fi
fi

# ── 5. Build the distributable zip ───────────────────────────────────────────
cd "$REPO"
rm -f Step2Blend-*.zip
zip -q -r "$ZIP_NAME" step_importer LICENSE LICENSES-third-party.txt \
    --exclude "*.pyc" --exclude "*/__pycache__/*" --exclude "*.DS_Store"

echo
echo "── Done ──────────────────────────────────────────────────────────────"
echo "Zip:   $REPO/$ZIP_NAME ($(du -h "$REPO/$ZIP_NAME" | cut -f1))"
if $INSTALL; then
    echo "Installed: ~/Library/Application Support/Blender/${BLENDER_VERSION}/scripts/addons/step_importer"
fi
