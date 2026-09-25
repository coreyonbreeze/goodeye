#!/usr/bin/env sh
# Install GoodEye: the `goodeye` command and the agent skill.
#   ./install.sh                  command in ~/.local/bin, skill in ~/.claude/skills (all projects)
#   ./install.sh --project DIR    skill in DIR/.claude/skills only (one project)
#   BIN_DIR=/usr/local/bin ./install.sh
set -eu

REPO="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
SKILLS_DIR="$HOME/.claude/skills"

while [ $# -gt 0 ]; do
  case "$1" in
    --project) SKILLS_DIR="$(cd "$2" && pwd)/.claude/skills"; shift 2 ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

command -v python3 >/dev/null 2>&1 || { echo "GoodEye needs python3 (3.9 or newer)." >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { echo "GoodEye needs Python 3.9 or newer." >&2; exit 1; }

mkdir -p "$BIN_DIR" "$SKILLS_DIR"
chmod +x "$REPO/goodeye.py"
ln -sf "$REPO/goodeye.py" "$BIN_DIR/goodeye"
# Symlink the skill so `git pull` updates it too.
rm -rf "$SKILLS_DIR/goodeye"
ln -s "$REPO/skills/goodeye" "$SKILLS_DIR/goodeye"

echo "Installed:"
echo "  command  $BIN_DIR/goodeye"
echo "  skill    $SKILLS_DIR/goodeye"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "Add $BIN_DIR to your PATH to run goodeye from anywhere." ;; esac
command -v ffprobe >/dev/null 2>&1 || echo "Optional: install ffmpeg (ffprobe) for video frame counts on the timeline."
echo
echo "Try it:  goodeye demo && goodeye open"
