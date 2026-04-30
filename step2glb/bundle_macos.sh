#!/bin/bash
# Collect all recursive dylib deps of step2glb from /opt/homebrew,
# copy them into ./bundle/lib, rewrite rpaths to @executable_path/../lib,
# and ad-hoc codesign everything. Produces a self-contained bundle.
set -euo pipefail

BIN_SRC="build/step2glb"
OUT="bundle"
rm -rf "$OUT"
mkdir -p "$OUT/bin" "$OUT/lib"

cp "$BIN_SRC" "$OUT/bin/step2glb"

collect_deps() {
    local target="$1"
    otool -L "$target" 2>/dev/null \
        | awk 'NR>1 {print $1}' \
        | { grep -E '^(/opt/homebrew|/usr/local|@rpath)' || true; }
}

# For an @rpath/foo.dylib reference, find the actual file. OCCT installs
# to /opt/homebrew/opt/opencascade/lib; NetGen we built into ./netgen_dist/lib.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
resolve_rpath() {
    local ref="$1"  # e.g. @rpath/libTKernel.7.9.dylib
    local base="${ref#@rpath/}"
    for dir in \
        "$SCRIPT_DIR/netgen_dist/lib" \
        /opt/homebrew/opt/opencascade/lib \
        /opt/homebrew/lib \
        /usr/local/opt/opencascade/lib \
        /usr/local/lib; do
        if [ -e "$dir/$base" ]; then
            echo "$dir/$base"
            return 0
        fi
    done
    return 1
}

COPIED_MARK="$OUT/.copied"
: > "$COPIED_MARK"

walk() {
    local target="$1"
    local deps
    deps=$(collect_deps "$target")
    local dep
    while IFS= read -r dep; do
        [ -z "$dep" ] && continue
        local base
        base=$(basename "$dep")
        if ! grep -qxF "$base" "$COPIED_MARK"; then
            echo "$base" >> "$COPIED_MARK"
            local src
            if [[ "$dep" == @rpath/* ]]; then
                src=$(resolve_rpath "$dep") || { echo "WARN: cannot resolve $dep"; continue; }
            else
                src="$dep"
            fi
            local real
            real=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$src")
            cp "$real" "$OUT/lib/$base"
            chmod u+w "$OUT/lib/$base"
            walk "$OUT/lib/$base"
        fi
    done <<< "$deps"
}

walk "$OUT/bin/step2glb"

echo "--- Copied $(ls "$OUT/lib" | wc -l | tr -d ' ') dylibs ---"

# Rewrite install names and dep paths for the binary + every dylib.
for f in "$OUT/bin/step2glb" "$OUT/lib"/*.dylib; do
    if [[ "$f" == *.dylib ]]; then
        install_name_tool -id "@executable_path/../lib/$(basename "$f")" "$f"
    fi
    # Rewrite every external dep to @executable_path/../lib/<basename>
    otool -L "$f" 2>/dev/null | awk 'NR>1 {print $1}' \
        | { grep -E '^(/opt/homebrew|/usr/local|@rpath)' || true; } \
        | while read -r dep; do
            [ -z "$dep" ] && continue
            install_name_tool -change "$dep" "@executable_path/../lib/$(basename "$dep")" "$f" 2>/dev/null || true
        done
done

# Ad-hoc codesign (required for arm64 binaries after patching).
# On macOS 15+ AMFI rejects ad-hoc signed third-party dylibs unless the
# loading binary has the disable-library-validation entitlement.
ENT="$OUT/entitlements.plist"
cat > "$ENT" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.cs.disable-library-validation</key>
    <true/>
    <key>com.apple.security.cs.allow-dyld-environment-variables</key>
    <true/>
    <key>com.apple.security.cs.allow-unsigned-executable-memory</key>
    <true/>
</dict>
</plist>
EOF

# Sign dylibs first, then the binary with entitlements last.
for f in "$OUT/lib"/*.dylib; do
    codesign --force --sign - --timestamp=none "$f"
done
codesign --force --sign - --timestamp=none \
    --options=runtime --entitlements "$ENT" \
    "$OUT/bin/step2glb"

echo "--- Bundle ready ---"
du -sh "$OUT"
