#!/bin/bash
# Provision Lantern's bounded container workspace from an operator-reviewed bundle.
set -euo pipefail
umask 077
bundle=$(realpath -e -- "${1:?Pass a full Git bundle with branch main}")
revision=${2:?Pass the expected full main revision}
[[ $EUID == 0 && $revision =~ ^[0-9a-f]{40}$ && -f $bundle ]] || exit 1
src=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
base=/var/lib/lanterncode
mountpoint=$base/work
unit=var-lib-lanterncode-work.mount

test -x /usr/bin/docker
test -x /usr/bin/python3.12
getent group lantern >/dev/null
install -d -m 0700 "$base" /etc/lantern /opt/lantern/coding
install -d -m 0700 "$mountpoint"
exec 9>"$base/install.lock"
flock -n 9 || { echo "Another coding installation is running." >&2; exit 1; }
temp=$(mktemp -d "$base/.install.XXXXXXXX")
seed_dir=
cleanup_stage() {
    rm -rf -- "$temp"
    if [[ -n $seed_dir ]]; then rm -rf --one-file-system -- "$seed_dir"; fi
}
trap cleanup_stage EXIT

if [[ ! -e $base/seeded ]]; then
    [[ $(git bundle list-heads "$bundle" refs/heads/main) == "$revision refs/heads/main" ]] || {
        echo "Bundle main does not match the reviewed revision." >&2; exit 1;
    }
fi

# Build in a minimal operator-owned context. The
# resulting content-addressed image ID, not a mutable tag, is used in production.
install -m 0600 "$src/deploy/coding/Dockerfile" "$temp/Dockerfile"
install -m 0600 "$src/deploy/coding/.dockerignore" "$temp/.dockerignore"
install -m 0644 "$src/deploy/coding/render_pdf.py" "$temp/render_pdf.py"
install -m 0644 "$src/deploy/coding/receive_import.py" "$temp/receive_import.py"
install -m 0644 "$src/deploy/coding/receive_clone.py" "$temp/receive_clone.py"
docker build --build-arg "BASE=${LANTERN_CODING_BASE:-python:3.12-slim-bookworm}" \
    --iidfile "$temp/image-id" --tag lantern-code:local "$temp"
image=$(cat "$temp/image-id")
[[ $image =~ ^sha256:[0-9a-f]{64}$ ]]

# Stage the one approved heritage PDF outside user-writable directories.
# The host-provided index pins the original bytes. The source is opened with
# the account's ordinary permissions; Docker never follows an account path.
heritage_pdf=/home/lantern/.local/state/lantern/heritage/lmstudio-chat.pdf
if sudo -n -u lantern test -e "$heritage_pdf" || sudo -n -u lantern test -L "$heritage_pdf"; then
    sudo -n -u lantern /usr/bin/python3.12 -I - > "$temp/heritage.pdf" <<'PY'
import hashlib, json, os, stat, sys
parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
for part in ("home", "lantern", ".local", "state", "lantern", "heritage"):
    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    os.close(parent)
    parent = child
def document(name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= limit:
        raise SystemExit("Invalid private heritage document.")
    return os.fdopen(fd, "rb")
with document("lmstudio-chat.pdf-pages.json", 16 * 1024 * 1024) as index:
    expected = json.load(index)["sha256"]
with document("lmstudio-chat.pdf", 64 * 1024 * 1024) as pdf:
    size = os.fstat(pdf.fileno()).st_size
    digest = hashlib.sha256()
    for remaining in range(size, 0, -65536):
        chunk = pdf.read(min(65536, remaining))
        digest.update(chunk)
        sys.stdout.buffer.write(chunk)
    if pdf.read(1) or digest.hexdigest() != expected:
        raise SystemExit("Private PDF does not match the heritage text companion.")
PY
    chmod 0444 "$temp/heritage.pdf"
fi

if [[ ! -f $base/work.ext4 ]]; then
    [[ ! -e $base/seeded && ! -L $base/work.ext4 ]]
    fallocate --length 256M "$temp/new.ext4"
    mkfs.ext4 -q -F -m 1 -L lanterncode "$temp/new.ext4"
    mv -T "$temp/new.ext4" "$base/work.ext4"
fi
install -m 0644 "$src/deploy/coding/$unit" "/etc/systemd/system/$unit"
systemctl daemon-reload
systemctl enable --now "$unit"
mountpoint -q "$mountpoint"
[[ $(findmnt -n -o FSTYPE --target "$mountpoint") == ext4 ]]
loopdev=$(findmnt -n -o SOURCE --target "$mountpoint")
[[ $(losetup --noheadings --output BACK-FILE --raw "$loopdev") == "$base/work.ext4" ]]

if [[ ! -e $base/seeded ]]; then
    # Never run host git or root shell commands in an agent-modifiable checkout.
    # Even the initial checkout takes place unprivileged and offline.
    if [[ -e $mountpoint/files ]]; then
        echo "Unmarked existing coding workspace. Inspect it before seeding." >&2
        exit 1
    fi
    seed_dir=$(mktemp -d "$mountpoint/.seed.XXXXXXXX")
    chown 65532:65532 "$seed_dir"
    # Bind only a copy for the seeding container. The host stage stays private.
    install -m 0444 "$bundle" "$temp/seed.bundle"
    docker run --rm --pull=never --network none --cap-drop=ALL \
        --security-opt=no-new-privileges=true --read-only --user=65532:65532 \
        --pids-limit=64 --memory=256m --memory-swap=256m --cpus=0.5 --log-driver=none \
        --tmpfs=/tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777 \
        -v "$seed_dir:/work:rw,z" -v "$temp/seed.bundle:/seed.bundle:ro,z" \
        -e HOME=/work/.home -e GIT_CONFIG_NOSYSTEM=1 --entrypoint /bin/sh "$image" \
        -eu -c '
            mkdir -p -m 0700 "$HOME/src" /work/lantern
            ln -s /work/lantern "$HOME/src/lantern"
            git clone --mirror /seed.bundle /work/lantern/.git
            cd /work/lantern
            git --git-dir=.git config core.bare false
            git symbolic-ref HEAD refs/heads/main
            test "$(git rev-parse HEAD)" = "$1"
            git fsck --full
            git config --remove-section remote.origin
            git checkout --force main
            git config user.name Lantern
            git config user.email lantern@localhost
            test -z "$(git remote)"
        ' seed "$revision"
    mv -T -- "$seed_dir" "$mountpoint/files"
    seed_dir=
    printf '%s\n' "$revision" > "$base/seeded"
fi

# Never interrupt an admitted action. The additional Docker check covers an
# older broker which did not yet take this lock. On that release, hold the
# chat worker's idle admission gate during the initial broker replacement.
exec 8>>"$base/action.lock"
flock -nx 8 || { echo "A Lantern sandbox request is active. Retry after it finishes." >&2; exit 1; }
active_container=$(docker ps -aq --filter 'label=org.lantern.coding-broker=1' --filter 'name=^/lantern-coding-command$')
[[ -z $active_container ]] || {
    echo "A Lantern coding container is active. Retry after it finishes." >&2; exit 1;
}
install -m 0644 "$src/lantern/coding_broker.py" /opt/lantern/coding/broker.py
install -d -o root -g root -m 0755 /var/lib/lantern-imports
install -d -o lantern -g lantern -m 0711 /var/lib/lantern-imports/objects
install -d -o lantern -g lantern -m 0711 /var/lib/lantern-imports/clone-packs
install -d -o 65532 -g 65532 -m 0700 "$mountpoint/clone-stage"
install -d -m 0755 /etc/systemd/system/lantern-chat.service.d
printf '%s\n' '[Service]' 'Environment=LANTERN_FILE_IMPORTS=1' 'Environment=LANTERN_REPO_CLONES=1' \
    'ReadWritePaths=/var/lib/lantern-imports/objects /var/lib/lantern-imports/clone-packs' > "$temp/file-imports.conf"
install -m 0644 "$temp/file-imports.conf" /etc/systemd/system/lantern-chat.service.d/file-imports.conf
if [[ -f $temp/heritage.pdf ]]; then
    mv -Tf -- "$temp/heritage.pdf" "$base/heritage.pdf"
elif [[ -f $base/heritage.pdf ]]; then
    rm -f -- "$base/heritage.pdf"
fi
install -m 0600 "$temp/image-id" /etc/lantern/coding-image
for file in lantern-code.socket lantern-code.service; do
    install -m 0644 "$src/deploy/coding/$file" "/etc/systemd/system/$file"
done
systemctl daemon-reload
systemctl enable --now lantern-code.socket
systemctl restart lantern-code.service
systemctl is-active --quiet lantern-code.service
printf 'Lantern coding broker ready; initial clone %s, image %s.\n' "$(cat "$base/seeded")" "$image"
