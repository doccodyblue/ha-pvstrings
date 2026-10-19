#!/bin/bash
# Vendor the dashboard card from ha-pvstrings-dash at a release tag.
#
# The card is developed in its own repository and shipped inside this one
# (HACS policy for a card that only works with one integration).  Copy a
# tagged release, never a working tree, and keep CARD_VERSION in card.py in
# step -- tests/test_card.py fails if the two disagree.
#
# Usage: tools/sync_dash.sh v0.22.1 [path/to/ha-pvstrings-dash]
set -euo pipefail
TAG="${1:?usage: $0 <tag> [dash repo]}"
DASH="${2:-$(dirname "$0")/../../ha-pvstrings-dash}"
cd "$(dirname "$0")/.."
OUT=custom_components/pvstrings/www/pvstrings-dash.js
git -C "$DASH" show "$TAG:pvstrings-dash.js" > "$OUT"
VERSION=$(sed -n 's/^const PVS_VERSION = "\(.*\)";$/\1/p' "$OUT")
[ "v$VERSION" = "$TAG" ] || { echo "tag $TAG carries PVS_VERSION $VERSION" >&2; exit 1; }
sed -i.bak "s/^CARD_VERSION = \".*\"$/CARD_VERSION = \"$VERSION\"/" custom_components/pvstrings/card.py
rm -f custom_components/pvstrings/card.py.bak
echo "vendored $TAG ($(wc -c < "$OUT") bytes)"
