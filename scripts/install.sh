#!/usr/bin/env bash
# Install hermes-antigravity-native into Hermes' venv and drop the thin file-plugin wrappers.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PY="${HERMES_PYTHON:-$HERMES_HOME/hermes-agent/venv/bin/python}"
UV="${UV:-$(command -v uv || echo "$HERMES_HOME/bin/uv")}"

"$UV" pip install --python "$PY" -e "$REPO"

mkdir -p "$HERMES_HOME/plugins/model-providers"
rm -rf "$HERMES_HOME/plugins/model-providers/antigravity-native" "$HERMES_HOME/plugins/antigravity-native-cli"
cp -r "$REPO/wrappers/model-providers/antigravity-native" "$HERMES_HOME/plugins/model-providers/antigravity-native"
cp -r "$REPO/wrappers/antigravity-native-cli" "$HERMES_HOME/plugins/antigravity-native-cli"
echo "installed. enable the CLI commands with: hermes plugins enable antigravity-native-cli"
