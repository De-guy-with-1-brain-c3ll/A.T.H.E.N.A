#!/usr/bin/env bash
# Finish a sleep-mode deployment on the Orange Pi.
#
# The signed updater only ships src/athena/**. Two things it deliberately does not
# touch have to be handled by hand:
#
#   1. /opt/athena/data/prompts/  OVERRIDES the packaged src/athena/system/*.txt.
#      If the dashboard has ever saved a prompt, the packaged copy is dead code and
#      the new tools are never described to the model.
#   2. The services have to be restarted to pick up the new release, and the sleep
#      status file has to exist for the first question to be answerable.
#
# Run this ON THE PI, as root, after the update service has installed a new release.
#
#   sudo bash /tmp/sync-sleep-prompts.sh
#
# It is safe to run more than once.

set -euo pipefail

DATA_DIR="${ATHENA_DATA_DIR:-/opt/athena/data}"
CURRENT="${ATHENA_ROOT:-/opt/athena}/current"
PROMPTS="${DATA_DIR}/prompts"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run me with sudo." >&2
    exit 1
fi

if [ ! -e "$CURRENT" ]; then
    echo "No installed release at $CURRENT. Has the updater run?" >&2
    exit 1
fi

VERSION="$(cat "$CURRENT/VERSION" 2>/dev/null || echo unknown)"
echo "Installed release: $VERSION"

PACKAGED="$CURRENT/src/athena/system"
if [ ! -d "$PACKAGED" ]; then
    echo "No packaged prompts at $PACKAGED." >&2
    exit 1
fi

# --- 1. Sync the editable prompt copies -------------------------------------
#
# Only overwrite a prompt that already exists, or that the user has asked for.
# Creating copies of every prompt would silently switch the Pi from "packaged
# prompts, updated by releases" to "pinned prompts, updated by nobody" -- which is
# how a Pi ends up running a prompt from six versions ago.
mkdir -p "$PROMPTS"

synced=0
for name in system memory; do
    packaged="${PACKAGED}/${name}_prompt.txt"
    editable="${PROMPTS}/${name}_prompt.txt"

    if [ ! -f "$packaged" ]; then
        echo "  ${name}: no packaged prompt, skipping"
        continue
    fi

    if [ ! -f "$editable" ]; then
        echo "  ${name}: no editable copy, so the packaged one is already in use -- nothing to do"
        continue
    fi

    if cmp -s "$packaged" "$editable"; then
        echo "  ${name}: already identical"
        continue
    fi

    # Keep the previous text so an edit made in the dashboard can be recovered.
    backup="${editable}.bak-$(date +%Y%m%d%H%M%S)"
    cp -p "$editable" "$backup"
    cp -p "$packaged" "$editable"
    echo "  ${name}: updated (previous copy saved as $(basename "$backup"))"
    synced=$((synced + 1))
done

# --- 2. Prove the prompt actually names the new tools ------------------------
if [ -f "$PROMPTS/system_prompt.txt" ]; then
    effective="$PROMPTS/system_prompt.txt"
else
    effective="${PACKAGED}/system_prompt.txt"
fi

echo "Effective system prompt: $effective"
for tool in sleep_mode sleep_status; do
    if grep -q "$tool" "$effective"; then
        echo "  mentions ${tool}: yes"
    else
        echo "  mentions ${tool}: NO -- the model will not know about it" >&2
    fi
done

# --- 3. Make sure the status directory exists --------------------------------
mkdir -p "$DATA_DIR"
chown -R athena:athena "$PROMPTS" "$DATA_DIR" 2>/dev/null || true

# --- 4. Restart whatever interfaces are enabled -------------------------------
restarted=0
for unit in athena-voice.service athena-feishu.service athena-web.service; do
    if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
        echo "Restarting $unit"
        systemctl restart "$unit"
        restarted=$((restarted + 1))
    fi
done

if [ "$restarted" -eq 0 ]; then
    echo "No ATHENA interface is enabled; start one when you are ready."
fi

# --- 5. Report -----------------------------------------------------------------
echo
echo "Done. ${synced} prompt(s) updated, ${restarted} service(s) restarted."
echo
echo "Check the consolidation status with:"
echo "  sudo -u athena $CURRENT/.venv/bin/athena-sleep --status"
echo
echo "Ask ATHENA in a chat:"
echo "  \"is your memory up to date\""
