#!/bin/bash
# Install the fixed-repository service and its separately privileged offline helper.
set -euo pipefail
umask 077
revision=${1:?Pass the full reviewed Git revision}
[[ $EUID == 0 && $revision =~ ^[a-f0-9]{40}$ ]]
src=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
base=/opt/lantern
test -s /etc/lantern/coding-image
test -e /var/lib/lanterncode/action.lock
mountpoint -q /var/lib/lanterncode/work
if ! getent passwd lantern-git >/dev/null; then
    useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /sbin/nologin lantern-git
fi
getent group lantern-git >/dev/null
if [[ ! -e /etc/lantern/github-token ]]; then
    install -o root -g root -m 0600 /dev/null /etc/lantern/github-token
fi
install -d -m 0755 "$base/github-releases"
stage=$(mktemp -d "$base/github-releases/.stage.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT
release=$base/github-releases/$revision
if [[ ! -d $release ]]; then
    mkdir "$stage/app"
    /usr/bin/python3.12 -I - "$src/lantern" "$stage/app/broker.pyz" <<'PY'
from pathlib import Path
import sys
from zipfile import ZipFile, ZIP_DEFLATED
with ZipFile(sys.argv[2], "x", ZIP_DEFLATED) as app:
    app.writestr("__main__.py", "import sys\nfrom lantern.github_broker import main\nmain(sys.argv[1])\n")
    app.writestr("lantern/__init__.py", "")
    for name in ("github_broker", "github_flow", "coding_broker"):
        app.write(Path(sys.argv[1]) / (name + ".py"), f"lantern/{name}.py")
PY
    install -m 0644 "$src/lantern/git_workspace.py" "$stage/app/git_workspace.py"
    chmod 0755 "$stage/app"
    chmod 0644 "$stage/app/broker.pyz"
    mv -T "$stage/app" "$release"
fi
ln -s "$release" "$stage/current"
mv -Tf "$stage/current" "$base/github"
install -m 0755 "$src/deploy/github/lantern-github-auth" /usr/local/sbin/lantern-github-auth
for name in lantern-github.socket lantern-github@.service lantern-git-work.socket lantern-git-work@.service; do
    install -m 0644 "$src/deploy/github/$name" "/etc/systemd/system/$name"
done
install -d -m 0755 /etc/systemd/system/lantern-chat.service.d
printf '%s\n' '[Service]' 'Environment=LANTERN_GITHUB_SOCKET=/run/lantern-github.sock' > "$stage/github.conf"
install -m 0644 "$stage/github.conf" /etc/systemd/system/lantern-chat.service.d/github.conf
systemctl daemon-reload
systemctl enable --now lantern-git-work.socket lantern-github.socket
systemctl is-active --quiet lantern-git-work.socket
systemctl is-active --quiet lantern-github.socket
printf 'GitHub services ready. Activate the matching worker at an idle boundary.\n'
