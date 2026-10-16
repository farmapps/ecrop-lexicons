#!/usr/bin/env bash
# Regenerate the lexicons from openapi/ecrop.yaml and test them with goat:
#  1. goat lex lint   - schema syntax, best practices and style
#  2. goat lex parse  - schema parsing
#  3. goat lex validate - every OpenAPI example, converted to a record, must be valid
#  4. negative tests  - records that violate the data model must be rejected
# Usage: GOAT=/path/to/goat tests/run.sh
set -uo pipefail
cd "$(dirname "$0")/.."
GOAT=${GOAT:-goat}
LEX=lexicons
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
fail=0
ok()  { echo "PASS $*"; }
bad() { echo "FAIL $*"; fail=1; }

rm -rf "$LEX"
python3 -I tools/openapi2lexicon.py openapi/ecrop.yaml "$LEX" >/dev/null || { echo "generation failed"; exit 1; }

"$GOAT" lex lint "$LEX" >"$TMP/lint" 2>&1 && ok "goat lex lint" || { cat "$TMP/lint"; bad "goat lex lint"; }
"$GOAT" lex parse $(find "$LEX" -name '*.json') >"$TMP/parse" 2>&1 && ok "goat lex parse" || { cat "$TMP/parse"; bad "goat lex parse"; }

# Positive: converted OpenAPI examples (list examples are split into their items).
mkdir -p examples/records
while IFS=$'\t' read -r ex nsid; do
  src=examples/openapi/$ex.json
  if [ "$(jq -r type "$src")" = array ]; then
    n=$(jq length "$src")
    for i in $(seq 0 $((n - 1))); do
      jq ".[$i]" "$src" >"$TMP/item.json"
      python3 -I tools/openapi2record.py "$LEX" "$nsid" "$TMP/item.json" >"examples/records/${ex}_$i.json" 2>/dev/null
      "$GOAT" lex validate -c "$LEX" "examples/records/${ex}_$i.json" >/dev/null 2>&1 \
        && ok "validate $ex[$i] as $nsid" || bad "validate $ex[$i] as $nsid"
    done
  else
    python3 -I tools/openapi2record.py "$LEX" "$nsid" "$src" >"examples/records/$ex.json" 2>/dev/null
    "$GOAT" lex validate -c "$LEX" "examples/records/$ex.json" >/dev/null 2>&1 \
      && ok "validate $ex as $nsid" || bad "validate $ex as $nsid"
  fi
done <examples/map.tsv

# Negative: must be rejected.
negative() {  # name, base record, jq filter
  jq "$3" "examples/records/$2.json" >"$TMP/neg.json"
  if "$GOAT" lex validate -c "$LEX" "$TMP/neg.json" >"$TMP/neg.out" 2>&1; then
    bad "accepted $1"
  else
    ok "rejects $1: $(grep '^error' "$TMP/neg.out")"
  fi
}
negative "float value"           plot_200 '.area.content = 1.5'
negative "missing scaleFactor"   plot_200 'del(.area.scaleFactor)'
negative "wrong field type"      plot_200 '.plotType = 5'
negative "missing required"      grower_200 'del(.name)'
negative "invalid datetime"      taskRequest '.startDateTime = "12-03-2025"'
negative "wrong geometry union"  plotFeatureTopLevel_200_rfc7946 '.geometry["$type"] = "org.farmapps.temp.ecrop.geo.defs#pointType"'
negative "ring too short"        plotFeatureTopLevel_200_rfc7946 '.geometry.coordinates[0].positions |= .[0:3]'

[ $fail = 0 ] && echo "ALL TESTS PASSED" || echo "SOME TESTS FAILED"
exit $fail
