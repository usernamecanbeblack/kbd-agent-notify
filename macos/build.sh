#!/usr/bin/env bash
# Build the macOS kbdflash helper.
#
# Requires the Xcode command line tools (provides swiftc):
#   xcode-select --install
#
# Produces ./kbdflash next to this script. No root, no signing needed.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v swiftc >/dev/null 2>&1; then
  echo "error: swiftc not found. Install Xcode command line tools:" >&2
  echo "  xcode-select --install" >&2
  exit 1
fi

echo "Building kbdflash..."
swiftc -O "$DIR/kbdflash.swift" -o "$DIR/kbdflash"
echo "Built: $DIR/kbdflash"
echo
echo "Current keyboard backlight state:"
"$DIR/kbdflash" read || true
echo
echo "Test a flash with:  $DIR/kbdflash flash --count 3"
