#!/usr/bin/env bash
# Prints what CodeQL found, in the job log and the job summary, so the findings can be read
# and fixed without opening Security > Code scanning. It never fails the job: GitHub's own
# "CodeQL" check on the pull request does that for new alerts.
# Usage: codeql-report.sh <directory CodeQL wrote its SARIF files to>
set -euo pipefail

findings=""
for sarif in "$1"/*.sarif; do
  [[ -s "$sarif" ]] || continue
  findings+="$(jq -r '
    .runs[]?.results[]? |
    "\(.ruleId // "unknown")  \(.locations[0].physicalLocation.artifactLocation.uri // ""):\(.locations[0].physicalLocation.region.startLine // "-")  \((.message.text // "") | gsub("\n"; "; "))"
  ' "$sarif")"$'\n'
done
findings="${findings%$'\n'}"

if [[ -z "$findings" ]]; then
  echo "CodeQL found nothing."
  exit 0
fi

echo "CodeQL findings:"
echo "$findings"

{
  echo "### CodeQL findings"
  echo
  echo '```'
  echo "$findings"
  echo '```'
} >>"${GITHUB_STEP_SUMMARY:-/dev/null}"
