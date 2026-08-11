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
echo "── Reminder ──────────────────────────────────────────────"
echo "In every dependent repo's requirements.txt, update the line to:"
echo "  trading-shared-data @ git+https://github.com/GkolfosGeorge/shared-data-layer.git@$TAG"
echo ""
echo "Then reinstall in each one with:"
echo "  \"/c/Users/georg/anaconda3/python.exe\" -m pip install -r requirements.txt --force-reinstall --no-deps"
