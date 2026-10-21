#!/bin/bash
# bump_version.sh — automates the commit + push + tag + push-tag sequence
# for the shared-data-layer repo.
#
# Usage (run from inside the shared-data-layer folder):
#   ./bump_version.sh 0.1.6 "Add fundamentals loader function"

set -e   # stop immediately if any command fails

VERSION="$1"
MESSAGE="$2"

if [ -z "$VERSION" ] || [ -z "$MESSAGE" ]; then
    echo "Usage: ./bump_version.sh <version> \"<commit message>\""
    echo 'Example: ./bump_version.sh 0.1.6 "Add fundamentals loader function"'
    exit 1
fi

TAG="v$VERSION"

# 1) Update the version line inside pyproject.toml automatically
sed -i "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml
echo "✅ pyproject.toml updated to version = \"$VERSION\""

# 2) Commit everything currently changed
git add .
git commit -m "$MESSAGE ($TAG)"
git push

# 3) Tag and push the tag
git tag "$TAG"
git push origin "$TAG"

echo ""
echo "✅ Done. Pushed $TAG."
echo ""
# 4) Move every project that uses trading-shared-data to the new tag:
#    requirements.txt / pyproject.toml in all Python Finance repos, commit + push,
#    and the local install (refused while Jupyter/VS Code are open -- WinError 5).
./update_pins.sh shared "$TAG"

echo ""
echo "If fundamentals.py changed: after the next weekly ingest, run"
echo "     diagnostics.integrity_report(eng)   -> must print ✅ OK"
