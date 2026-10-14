#!/usr/bin/env bash
# Install hermes-antigravity-native into Hermes' venv and drop the thin file-plugin wrappers.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PY="${HERMES_PYTHON:-$HERMES_HOME/hermes-agent/venv/bin/python}"
UV="${UV:-$(command -v uv || echo "$HERMES_HOME/bin/uv")}"
PROVIDERS="$HERMES_HOME/plugins/model-providers"

"$UV" pip install --python "$PY" -e "$REPO"

# The provider wrapper dir is named "0-antigravity-native" on purpose: Hermes imports
# model-provider dirs in sorted order, and the legacy "antigravity" plugin imports
# hermes_cli.auth while loading, which snapshots the provider registry. Loading first
# keeps `--provider antigravity-native` resolvable without patching Hermes.
mkdir -p "$PROVIDERS"
rm -rf "$PROVIDERS/antigravity-native" "$PROVIDERS/0-antigravity-native" "$HERMES_HOME/plugins/antigravity-native-cli"
cp -r "$REPO/wrappers/model-providers/0-antigravity-native" "$PROVIDERS/0-antigravity-native"
cp -r "$REPO/wrappers/antigravity-native-cli" "$HERMES_HOME/plugins/antigravity-native-cli"
echo "installed. enable the CLI commands with: hermes plugins enable antigravity-native-cli"
echo "then verify with: hermes antigravity-native doctor"
