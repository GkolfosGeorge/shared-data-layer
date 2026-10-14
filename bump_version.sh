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
echo "EVERY repo pins a TAG in requirements.txt -- including the Actions-only"
echo "ones. A fresh install on GitHub still installs the PINNED tag, not main."
echo "(Oct 2026: daily-market-data stayed on v0.1.21 after v0.1.24 shipped, and"
echo " its weekly run re-inserted 1,792 rows the v0.1.24 repair had removed.)"
echo ""
echo "1) In EVERY repo's requirements.txt (daily-market-data, single-asset-analyzer,"
echo "   mean_reversion_script, ...), update the line to:"
echo "     trading-shared-data @ git+https://github.com/GkolfosGeorge/shared-data-layer.git@$TAG"
echo "   then commit + push. For Actions-only repos (daily-market-data) that's all;"
echo "   pull --rebase first if push is rejected -- its workflows commit daily."
echo ""
echo "2) In each LOCAL repo (Git Bash, cd into the repo first):"
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
echo ""
echo "5) Check nothing was missed -- every line must end in @$TAG:"
echo "     grep -H shared-data-layer \"/c/Users/georg/Google Drive/Python Finance\"/*/requirements.txt"
echo ""
echo "6) If fundamentals.py changed: after the next weekly ingest, run"
echo "     diagnostics.integrity_report(eng)   -> must print ✅ OK"
