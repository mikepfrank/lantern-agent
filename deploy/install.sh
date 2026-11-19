#!/bin/bash
# Install one reviewed git archive on Amazon Linux 2023. Run as the operator.
set +x
set -euo pipefail
umask 022

revision=${1:?Pass the full Git revision from the staged source archive}
archive=${2:-/home/ec2-user/chess-security-stage-20261115/codex/codex-package-x86_64-unknown-linux-musl.tar.gz}
[[ $EUID == 0 && $revision =~ ^[0-9a-f]{40}$ ]] || {
    echo "Run as root with a full lowercase Git revision." >&2
    exit 1
}
src=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
base=/opt/lantern
runtime=$base/runtime/codex-0.169.0
release=$base/releases/$revision
state=/home/lantern/.local/state/lantern
unit=lantern-chat.service
digest=3b8ed091149cc63a59046b9f094bcfa21cdb4dda15ede441ce369bcee4a71ba0

test -f "$src/lantern/__main__.py"
test -f "$src/lantern/personas/lantern-v1.md"
test -x /usr/bin/python3.12
install -d -m 0755 "$base" "$base/releases" "$base/runtime"
install -d -m 0700 /etc/lantern
exec 9>"$base/.install.lock"
flock -n 9 || { echo "Another Lantern installation is in progress." >&2; exit 1; }
stage=$(mktemp -d "$base/.install.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT

# Create or verify the operator's normal state path as the unprivileged user.
sudo -n -u lantern install -d -m 0700 "$state"
[[ "$(realpath -e -- "$state")" == "$state" ]]
[[ "$(stat -c '%U' -- "$state")" == lantern ]]

if [[ ! -d $runtime ]]; then
    install -m 0600 -- "$archive" "$stage/codex.tar.gz"
    printf '%s  %s\n' "$digest" "$stage/codex.tar.gz" | sha256sum --check --status
    install -d -m 0755 "$stage/runtime"
    tar --no-same-owner --no-same-permissions -xzf "$stage/codex.tar.gz" -C "$stage/runtime"
    chmod -R go-w "$stage/runtime"
    test -x "$stage/runtime/bin/codex"
    mv -T -- "$stage/runtime" "$runtime"
fi
[[ "$(sudo -n -u lantern "$runtime/bin/codex" --version)" == 'codex-cli 0.169.0' ]]

if [[ ! -d $release ]]; then
    install -d -m 0755 "$stage/release"
    /usr/bin/python3.12 -I - "$src" "$stage/release/lantern.pyz" <<'PY'
from pathlib import Path
import sys
from zipfile import ZipFile, ZIP_DEFLATED

src = Path(sys.argv[1])
with ZipFile(sys.argv[2], "x", ZIP_DEFLATED) as app:
    app.writestr("__main__.py", "import runpy\nrunpy.run_module('lantern', run_name='__main__')\n")
    for path in sorted((src / "lantern").rglob("*")):
        if path.suffix in {".py", ".md"} and path.is_file():
            app.write(path, path.relative_to(src).as_posix())
PY
    chmod 0644 "$stage/release/lantern.pyz"
    mv -T -- "$stage/release" "$release"
fi
test -r "$release/lantern.pyz"
sudo -n -u lantern /usr/bin/python3.12 -I "$release/lantern.pyz" worker --help >/dev/null

# Run the login shell as lantern. Only these three values enter the service
# environment; machine-readable secret values never go into arguments or logs.
install -m 0600 /dev/null "$stage/worker.env"
(
    for name in OPENROUTER_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY; do
        value=$(sudo -n -iu lantern /usr/bin/printenv "$name") || {
            echo "Missing $name in the lantern login environment." >&2
            exit 1
        }
        [[ $value =~ ^[A-Za-z0-9_.:/+=-]+$ ]] || {
            echo "Missing or invalid $name in the lantern login environment." >&2
            exit 1
        }
        printf '%s=%s\n' "$name" "$value"
    done
) > "$stage/worker.env"

ln -s -- "$release" "$stage/current"
# Future upgrades should be scheduled when the worker is idle.
if systemctl is-active --quiet "$unit"; then
    systemctl stop "$unit"
fi
install -o root -g root -m 0600 "$stage/worker.env" /etc/lantern/worker.env
install -o root -g root -m 0755 "$src/deploy/lantern" /usr/local/bin/lantern
install -o root -g root -m 0644 "$src/deploy/$unit" "/etc/systemd/system/$unit"
mv -Tf -- "$stage/current" "$base/current"
systemctl daemon-reload
systemctl enable --now "$unit"
systemctl is-active --quiet "$unit"
printf 'Lantern release %s active; run lantern in the lantern account.\n' "$revision"
