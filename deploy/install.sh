#!/bin/bash
# Install one reviewed git archive on Amazon Linux 2023. Run as the operator.
set +x
set -euo pipefail
umask 022

worker_only=false
if [[ ${1:-} == --worker-only ]]; then
    worker_only=true
    shift
fi
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
if $worker_only && [[ ! -L $base/current || ! -f $base/current/lantern.pyz || ! -f /etc/systemd/system/$unit ]]; then
    echo "A worker-only update requires an existing Lantern installation." >&2
    exit 1
fi
stage=$(mktemp -d "$base/.install.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT

# Create or verify the operator's normal state path as the unprivileged user.
sudo -n -u lantern install -d -m 0700 "$state"
[[ "$(realpath -e -- "$state")" == "$state" ]]
[[ "$(stat -c '%U' -- "$state")" == lantern ]]
for file in Lantern.md lmstudio-chat.md lmstudio-chat.txt lmstudio-chat.pdf lmstudio-chat.pdf-pages.json; do
    sudo -n -u lantern test -r "$state/heritage/$file" || {
        echo "Missing private heritage file: $file" >&2
        exit 1
    }
done

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
        asset = path.parent == src / "lantern/web_assets" and path.suffix in {".css", ".html", ".js"}
        if (path.suffix in {".py", ".md"} or asset) and path.is_file():
            app.write(path, path.relative_to(src).as_posix())
PY
    chmod 0644 "$stage/release/lantern.pyz"
    mv -T -- "$stage/release" "$release"
fi
test -r "$release/lantern.pyz"
sudo -n -u lantern /usr/bin/python3.12 -I "$release/lantern.pyz" worker --help >/dev/null
# The code broker has its own installer and stays outside the worker's privileges.
systemctl is-active --quiet lantern-code.socket || {
    echo "Install the coding broker with deploy/install-coding.sh before activating this release." >&2
    exit 1
}

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

ln -s -- "$release" "$stage/worker-current"
if ! $worker_only; then
    ln -s -- "$release" "$stage/current"
fi
# Hold queue admission across the idle check and stop. The database connection
# belongs to lantern and closes when the installer's pipe closes, even on error.
if systemctl is-active --quiet "$unit"; then
    coproc IDLE {
        sudo -n -u lantern /usr/bin/python3.12 -I -c '
import sqlite3, sys
with sqlite3.connect(sys.argv[1], timeout=5) as db:
    db.execute("BEGIN IMMEDIATE")
    busy, queued = db.execute("""SELECT EXISTS(SELECT 1 FROM batches WHERE status=?),
                                 EXISTS(SELECT 1 FROM inbox WHERE batch_id IS NULL)""", ("active",)).fetchone()
    print("busy" if busy or queued else "idle", flush=True)
    sys.stdin.readline()
' "$state/chat.sqlite"
    }
    guard_pid=$IDLE_PID
    read -r -t 10 idle <&"${IDLE[0]}"
    if [[ $idle != idle || $(systemctl show "$unit" -p TasksCurrent --value) != 1 ]]; then
        echo "Lantern is processing work or settling usage. Retry the installation when idle." >&2
        exit 1
    fi
    systemctl stop "$unit"
    printf '\n' >&"${IDLE[1]}"
    wait "$guard_pid"
fi
install -o root -g root -m 0600 "$stage/worker.env" /etc/lantern/worker.env
install -o root -g root -m 0755 "$src/deploy/lantern" /usr/local/bin/lantern
install -o root -g root -m 0644 "$src/deploy/$unit" "/etc/systemd/system/$unit"
mv -Tf -- "$stage/worker-current" "$base/worker-current"
if ! $worker_only; then
    install -o root -g root -m 0644 "$src/deploy/lantern-web.service" /etc/systemd/system/
    mv -Tf -- "$stage/current" "$base/current"
fi
systemctl daemon-reload
systemctl enable --now "$unit"
systemctl is-active --quiet "$unit"
if $worker_only; then
    printf 'Lantern worker release %s active.\n' "$revision"
    exit 0
fi
systemctl enable lantern-web.service
systemctl restart lantern-web.service
systemctl is-active --quiet lantern-web.service
printf 'Lantern release %s active; run lantern in the lantern account.\n' "$revision"
