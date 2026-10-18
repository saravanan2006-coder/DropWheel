#!/usr/bin/env bash
# DropWheel installer for Fedora KDE Plasma
set -e
here="$(cd "$(dirname "$0")" && pwd)"

echo "==> Installing dependencies"
sudo dnf install -y python3-pyside6 ImageMagick poppler-utils ghostscript pandoc \
    libreoffice-writer libnotify
# Video/audio needs the full ffmpeg (libx264, etc.) from RPM Fusion. If you don't have it yet:
#   sudo dnf install https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm
#   sudo dnf swap ffmpeg-free ffmpeg --allowerasing

echo "==> Installing dropwheel"
mkdir -p ~/.local/bin ~/.local/share/applications ~/.local/share/kio/servicemenus
install -m 755 "$here/dropwheel.py" ~/.local/bin/dropwheel

cat > ~/.local/share/applications/dropwheel.desktop <<EOF
[Desktop Entry]
Type=Application
Name=DropWheel
Comment=Drop files on a wheel to convert them
Exec=$HOME/.local/bin/dropwheel --show
Icon=transform-move
Categories=Utility;
X-KDE-Shortcuts=Meta+Shift+W
EOF

cat > ~/.local/share/kio/servicemenus/dropwheel.desktop <<EOF
[Desktop Entry]
Type=Service
MimeType=all/allfiles;
Actions=dropwheel;

[Desktop Action dropwheel]
Name=Convert with DropWheel
Icon=transform-move
Exec=$HOME/.local/bin/dropwheel %F
EOF
chmod +x ~/.local/share/kio/servicemenus/dropwheel.desktop

# start the daemon at login so the hotkey is instant
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/dropwheel-daemon.desktop <<EOF
[Desktop Entry]
Type=Application
Name=DropWheel daemon
Exec=$HOME/.local/bin/dropwheel --daemon
EOF

kbuildsycoca6 >/dev/null 2>&1 || true
nohup ~/.local/bin/dropwheel --daemon >/dev/null 2>&1 &

echo
echo "Done. Hotkey: Meta+Shift+W (if it doesn't work, add it manually:"
echo "System Settings > Keyboard > Shortcuts > Add New > Command or Script: dropwheel --show)"
