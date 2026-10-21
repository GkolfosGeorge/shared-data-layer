#!/bin/bash
# update_pins.sh — after a new tag, move EVERY project that uses it to that tag, in one go.
#
# Usage (Git Bash, from anywhere):
#   ./update_pins.sh shared v0.1.26          # new trading-shared-data tag
#   ./update_pins.sh saa    v0.1.2           # new single-asset-analyzer tag (the daily mail)
#   ./update_pins.sh shared v0.1.26 --dry    # show what would change, write nothing
#
# What it does:
#   1. checks the tag really exists on GitHub (a pin to an unpushed tag breaks Actions)
#   2. finds every file in Python Finance/*/ that pins that package
#      (requirements.txt, pyproject.toml, .github/workflows/*.yml) and rewrites the tag
#   3. per repo: commits ONLY those files, pull --rebase, push
#      (other uncommitted work in the repo is left alone)
#   4. shared only: reinstalls ONLY trading-shared-data locally -- and refuses if
#      a python.exe is running (Jupyter/VS Code), since that causes WinError 5
#   5. prints every pin so you can see they all match
#
# New projects are picked up automatically: any Python Finance/<folder>/ that pins
# the package is included. No list to maintain.

ROOT="${PINS_ROOT:-/c/Users/georg/Google Drive/Python Finance}"
PY="/c/Users/georg/AppData/Local/Programs/Python/Python314/python.exe"
GH="https://github.com/GkolfosGeorge"

WHAT="$1"; TAG="$2"; DRY=""
[ "$3" = "--dry" ] && DRY=1

usage() { echo "Usage: ./update_pins.sh shared|saa vX.Y.Z [--dry]"; exit 1; }
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || usage

case "$WHAT" in
  shared) REPO="shared-data-layer";     PKG="trading-shared-data" ;;
  saa)    REPO="single-asset-analyzer"; PKG="single-asset-analyzer" ;;
  *)      usage ;;
esac
PATTERN="$REPO\.git@v[0-9]+\.[0-9]+\.[0-9]+"
NEW="$REPO.git@$TAG"

# 1) the tag must exist on GitHub
if [ -z "$(git ls-remote --tags "$GH/$REPO.git" "refs/tags/$TAG")" ]; then
    echo "❌ Το tag $TAG δεν υπάρχει στο GitHub ($REPO). Έκανες push το tag;"
    exit 1
fi
echo "✅ $REPO $TAG υπάρχει στο GitHub"
[ -n "$DRY" ] && echo "   (DRY RUN — τίποτα δεν γράφεται)"
echo ""

cd "$ROOT" || exit 1

# 2) find and rewrite every pin
declare -A CHANGED   # repo folder -> list of files
for f in */requirements.txt */pyproject.toml */.github/workflows/*.yml; do
    [ -f "$f" ] || continue
    grep -Eq "$PATTERN" "$f" || continue
    old=$(grep -Eo "$PATTERN" "$f" | head -1 | sed 's/.*@//')
    if [ "$old" = "$TAG" ]; then
        echo "   ήδη $TAG   $f"
        continue
    fi
    echo "   $old → $TAG   $f"
    [ -z "$DRY" ] && sed -E -i "s#$PATTERN#$NEW#g" "$f"
    dir="${f%%/*}"
    CHANGED[$dir]="${CHANGED[$dir]} ${f#*/}"
done
echo ""

# 3) commit + push, one repo at a time; a failure in one does not stop the others
FAILED=()
if [ -z "$DRY" ]; then
    for dir in "${!CHANGED[@]}"; do
        echo "── $dir"
        files=(${CHANGED[$dir]})
        if ( cd "$dir" \
             && git add -- "${files[@]}" \
             && git commit -q -m "Pin $PKG to $TAG" -- "${files[@]}" \
             && git pull -q --rebase --autostash \
             && git push -q ); then
            echo "   ✅ commit + push"
        else
            echo "   ❌ απέτυχε — δες το μήνυμα παραπάνω"
            FAILED+=("$dir")
        fi
    done
    [ ${#CHANGED[@]} -eq 0 ] && echo "Τίποτα για αλλαγή."
    echo ""
fi

# 4) local install (only the shared package; the analyzer runs from its own folder)
if [ "$WHAT" = "shared" ] && [ -z "$DRY" ]; then
    if tasklist 2>/dev/null | grep -qi "^python.exe"; then
        echo "⚠️  Τρέχει python.exe (Jupyter / VS Code). ΔΕΝ έκανα την τοπική εγκατάσταση."
        echo "   Κλείσ' τα (ή: taskkill //F //IM python.exe) και ξανατρέξε την ίδια εντολή:"
        echo "   τα pins είναι ήδη ενημερωμένα, θα κάνει μόνο την εγκατάσταση."
    else
        "$PY" -m pip install -q --force-reinstall --no-deps "$PKG @ git+$GH/$REPO.git@$TAG" \
          && echo "✅ τοπικά: $("$PY" -m pip show "$PKG" | grep ^Version)" \
          || FAILED+=("local install")
    fi
    echo ""
fi

# 5) every pin, so you can see they match
echo "── Όλα τα pins του $PKG:"
grep -HEo "$PATTERN" */requirements.txt */pyproject.toml */.github/workflows/*.yml 2>/dev/null \
  | sed 's#^#   #'
echo ""
if [ ${#FAILED[@]} -gt 0 ]; then
    echo "❌ Θέλουν προσοχή: ${FAILED[*]}"
    exit 1
fi
[ -z "$DRY" ] && [ "$WHAT" = shared ] && echo "✅ Έτοιμο. Αν άλλαξε το fundamentals.py: integrity_report μετά το επόμενο weekly ingest."
exit 0
