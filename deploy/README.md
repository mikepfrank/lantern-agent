# Lightsail terminal pilot

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

## Editable source

The operator can provision a full writable Git clone in `~lantern/src/lantern` from a full bundle. Check whether a clone is already present before creating it. On later releases, preserve its commits and working tree. The installed application runs from the separate, operator-owned release. The systemd worker's filesystem view does not include this checkout. The production agent container, in-container checkout, and GitHub PR submission remain later deployment work.
