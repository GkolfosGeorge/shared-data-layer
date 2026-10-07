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
echo "1) In every LOCAL repo's requirements.txt (single-asset-analyzer,"
echo "   mean_reversion_script, ...), update the line to:"
echo "     trading-shared-data @ git+https://github.com/GkolfosGeorge/shared-data-layer.git@$TAG"
echo "   (daily-market-data needs nothing -- it's Actions-only, always fresh)"
echo ""
echo "2) In each of those repos (Git Bash, cd into the repo first):"
echo "     \"/c/Users/georg/AppData/Local/Programs/Python/Python314/python.exe\" -m pip install -r requirements.txt --force-reinstall --no-deps"
echo "   (this is the single shared python.exe behind the 'Python 3.14 (finance)'"
echo "    Jupyter kernel -- not a conda env, no activation needed)"
echo ""
echo "3) Verify it took:"
echo "     \"/c/Users/georg/AppData/Local/Programs/Python/Python314/python.exe\" -m pip show trading-shared-data"
echo "   should show Version: ${VERSION}"
echo ""
echo "4) Restart the kernel in any already-open notebook before re-running cells"
echo "   (Jupyter loaded the old code into memory and won't see the update otherwise)"