#!/bin/zsh
# Generate the real plist from the template (with this machine's project path), install it into
# ~/Library/LaunchAgents and load it. Usage: scheduler/install.sh [install|uninstall]
set -e
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.prathikkumar.jobagent.daily"
GENERATED="$PROJECT_DIR/scheduler/$LABEL.plist"          # gitignored
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

case "${1:-install}" in
  install)
    mkdir -p "$PROJECT_DIR/data/clean" "$HOME/Library/LaunchAgents"
    sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$PROJECT_DIR/scheduler/$LABEL.plist.template" > "$GENERATED"
    plutil -lint "$GENERATED"
    cp "$GENERATED" "$TARGET"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$TARGET"
    launchctl print "$DOMAIN/$LABEL" | head -5
    echo "Installed. Runs daily at 8:30 AM. Log: $PROJECT_DIR/data/clean/scheduler.log"
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$TARGET" "$GENERATED"
    echo "Uninstalled."
    ;;
  *) echo "usage: $0 [install|uninstall]"; exit 2 ;;
esac
