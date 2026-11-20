# Lightsail private chat and coding pilot

The systemd worker serves the private `lantern` account over its local SQLite chat queue. The operator can leave the SSH session and return later. There is no public listener, DNS record, or reverse proxy for this pilot.

## Install a reviewed revision

Use an archive from the local repository. Record the full revision and the archive digest before uploading it to a private staging directory owned by the Lightsail administrator.

On a fresh host restore the private `heritage/` directory separately under `/home/lantern/.local/state/lantern/`. Use owner `lantern`, directory mode `0700`, and file mode `0600`. The operator's original files are `Lantern.md`, `lmstudio-chat.md`, `lmstudio-chat.txt` and `lmstudio-chat.pdf`. The page text companion `lmstudio-chat.pdf-pages.json` is UTF-8 JSON with `sha256` for the original PDF bytes and `pages` containing one extracted string per page, in order. The service mounts this folder read-only. It belongs outside the release archive and Git bundles.

```sh
git rev-parse HEAD
git archive --format=tar.gz -o lantern-src.tar.gz HEAD
```

After verifying the transferred digest and extracting the archive as the administrator, invoke the installer in that staging tree.

```sh
sudo bash deploy/install-coding.sh /absolute/path/lantern.bundle FULL_GIT_REVISION
sudo bash deploy/install.sh FULL_GIT_REVISION
sudo systemctl status --no-pager lantern-chat.service
sudo -iu lantern lantern status
```

The installer uses the verified Codex 0.169.0 archive staged during the host maintenance. An optional second argument selects another copy of that same archive, with the digest checked on first installation. It installs the complete runtime under `/opt/lantern/runtime/` and the Python app with its persona under `/opt/lantern/releases/<revision>/`. The worker and operator command use `/opt/lantern/current`. The private state remains in `/home/lantern/.local/state/lantern`, including the existing usage and persona records.

The installer obtains the three API keys in a login shell running as `lantern` and writes only those values to `/etc/lantern/worker.env` with root-only permissions. The unit reads this file before starting the worker as `lantern`. Codex receives the loopback token from the worker. The unit mounts the system read-only, hides the home directories except for the bound state directory, and disallows privilege elevation.

For an update, allow the current turn to finish before running the installer. It stops this worker, installs the release, switches the symlink and restarts the unit. A release rollback uses the same installer with the earlier reviewed revision and matching source. To refresh rotated keys, rerun the installer at an idle boundary.

After steering a turn, its superseded provider request can continue reporting usage in the background even after the replacement reply is visible. Allow that settlement to finish before a planned restart. A forced stop retains any unresolved reservation against the admission day's budget.

## Chat

From the laptop's normal terminal:

```sh
ssh -t lightsail "sudo -iu lantern lantern"
```

Within an existing `lantern` shell, run `lantern`. Use `lantern status`, `lantern history`, and `lantern usage` for one-shot checks. The system unit is enabled across boots. Worker logs are available to the administrator with `sudo journalctl -u lantern-chat.service`.

The unit passes `--heartbeat-minutes 60`. Its first independent start adds the new tool definitions to the current private Codex room under the exclusive worker lock. It preserves the pinned persona, messages and thread ID. It saves the original native history in `operator-backups/independent-work-v1/` in the private state directory before updating the registry and offsets. A later restart uses the saved cadence. Inspect it with `lantern schedule`. The worker reads and writes project files under `workspace/files/` through supervised tools.

## Browser

Use the [SSH command in the project README](../README.md#talk-to-lantern-on-lightsail) on the laptop. The local and remote ports must both be 8765. Only the private SSH tunnel carries browser traffic. The web service listens on host loopback, issues a fresh key at startup, and saves it in the owner-only `web-key` file in Lantern's private state. The operator link passes the key in the URL fragment. The browser clears it from the address bar and authenticates API requests from the tab. To rotate access, restart `lantern-web.service` and get a fresh link. Starting or stopping the web service does not interrupt the model worker.

## Coding environment

For a new installation, make the seed with `git bundle create lantern.bundle --branches --tags` from the reviewed repository. The coding installer builds a dedicated Python and Git image. Only the broker can reach Docker. The worker sends approved commands over `/run/lantern-code.sock`. Docker uses a pinned local image ID and starts a fresh unprivileged container with its own process namespace, offline networking, a read-only root and a writable coding volume. Each command gets 30 seconds, half a CPU, 256 MiB of RAM, 64 processes and 16 KiB of output. The 256 MiB ext4 volume is backed by `/var/lib/lanterncode/work.ext4` and mounts across boots.

The full editable clone is `/work/lantern`, also available as `~/src/lantern` inside the container. This differs from the operator checkout in the host's `~lantern/src/lantern`. The installer seeds the coding clone once. Subsequent service updates preserve branches and uncommitted work. GitHub publication needs a separate host-side proposal tool. The running service loads the root-owned release under `/opt/lantern/current`.

From the host's normal `ec2-user` shell, `sudo ls -la /var/lib/lanterncode/work/files` lists Lantern's persistent `/work`. Append `/lantern` to browse its checkout. For an interactive read-only view as the workspace owner, start a separate container on that volume.

```sh
sudo docker run --rm -it --pull=never --network none --cap-drop ALL \
  --security-opt no-new-privileges --read-only \
  -v /var/lib/lanterncode/work/files:/work:ro \
  -w /work/lantern lantern-code:local
```

Use `exit` to close the inspection shell. The volume remains available between Lantern's own coding commands.

The first worker start with coding enabled backs up the existing native room in `operator-backups/coding-workspace-v1/` before adding the tool. The worker, browser and broker can be inspected separately with `systemctl status lantern-chat lantern-web lantern-code.socket lantern-code.service`. For a coding-only outage, recover the mount and broker before retrying. Check for completed edits if a command lost its response.
