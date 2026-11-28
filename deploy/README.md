# Lightsail private chat and coding pilot

The systemd worker serves the private `lantern` account over its local SQLite chat queue. The private SSH room and the optional administrator HTTPS room use that same queue.

## Install a reviewed revision

For routine worker updates on the existing Lightsail host, sign in as `ec2-user` and run `/usr/local/bin/lantern-update-worker`. From the laptop, use `ssh lightsail /usr/local/bin/lantern-update-worker`. The command fetches the public repository's `main`, runs the regression suite, and invokes the worker-only installer at an idle boundary. If the worker is busy, rerun the command after the current turn and usage settlement finish. If the running worker already matches `main`, the command exits immediately after the version check. Pass a full commit SHA to require that exact revision at the tip of `main`.

The operator command is installed from `deploy/lantern-update-worker` with `sudo install -o root -g root -m 0755 deploy/lantern-update-worker /usr/local/bin/lantern-update-worker`. Use the component-specific instructions below for browser, proxy, or broker changes.

### First release with compaction notices

Deploy the worker and both web services together using the full installer below. Keep the UTC switchover timestamp. In an existing room, preview the historical checkpoints with `sudo -u lantern /usr/local/bin/lantern compaction-annotations --check --before "$cutover"`, where `cutover` is that timestamp in ISO format. Check the reported count and placement, then repeat with `--apply`. Another `--check` should report zero additions. The first application saves the boundary for future runs. The import adds a separate set of retrospective records to the room database and preserves original event IDs and native checkpoints.

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

The installer uses the verified Codex 0.169.0 archive staged during the host maintenance. An optional archive argument after the revision selects another copy of that same archive, with the digest checked on first installation. It installs the complete runtime under `/opt/lantern/runtime/` and the Python app with its persona under `/opt/lantern/releases/<revision>/`. The launcher resolves the selected archive at process startup. Worker, review administration, usage and administrator sign-in commands use `/opt/lantern/worker-current` when installed. The SSH browser and its link command use `/opt/lantern/current`. The private state remains in `/home/lantern/.local/state/lantern`, including the existing usage and persona records.

The installer obtains the three API keys in a login shell running as `lantern` and writes only those values to `/etc/lantern/worker.env` with root-only permissions. The unit reads this file before starting the worker as `lantern`. Codex receives the loopback token from the worker. The unit mounts the system read-only, hides the home directories except for the bound state directory, and disallows privilege elevation.

For an update, allow the current turn to finish before running the installer. A full deployment updates both release links and restarts both services, rotating the browser key. For a backend-only change compatible with the running browser and CLI, run `sudo bash deploy/install.sh --worker-only FULL_GIT_REVISION`. It uses the same idle check, switches the worker release and preserves the running browser session. A release rollback uses the installer from the matching reviewed source. To refresh rotated provider keys, rerun the installer at an idle boundary.

After steering a turn, its superseded provider request can continue reporting usage in the background even after the replacement reply is visible. Allow that settlement to finish before a planned restart. A forced stop retains any unresolved reservation against the admission day's budget.

## Live Sonnet reviewer comparison

Opus 3 decides each live referral. The private room worker can enqueue the evidence from a completed, audited adjudication for a separate process to send to Anthropic's Sonnet 4.6. Sonnet sees the same case and operator permissions and returns a structured judgment. Its answer is available only to the operator. Cases in which Opus requested research are recorded as exclusions because the reviewers would have seen different evidence. Voluntary consultations and Luna-only approvals do not enter the comparison.

To activate the seven-day pilot on an installed worker, run these commands on the host. Starting the same campaign again preserves its original deadline, sample, and spend.

```sh
sudo -iu lantern lantern reviewer-pilot start
sudo systemctl enable --now lantern-review-shadow.service
sudo -iu lantern lantern reviewer-pilot status
```

The pilot admits up to 20 referrals. Its total Sonnet reservation and spend ceiling is $0.50, within the existing supervisor budget. Admission also preserves $0.50 of the current UTC day's allowance for live review. A request whose delivery is uncertain retains its reservation. A worker restart cannot resend a paid request with the same ID.

After the collector has closed and any in-flight call has settled, the operator can grant one more observation window. Each grant admits up to ten new referrals and adds $0.50 to the Sonnet costs and outstanding reservations already on the ledger. Choose a deadline with a timezone within the next 24 hours. For example, this window ends at midnight Chicago time after November 21, 2026.

```sh
sudo -iu lantern lantern reviewer-pilot extend --until 2026-11-22T06:00:00Z
sudo systemctl restart lantern-review-shadow.service
sudo -iu lantern lantern reviewer-pilot status
```

Repeating the same deadline preserves the original grant even after it expires. Status, results, case IDs, and spending cover both windows. Previously skipped cases stay in the record. The same daily supervisor budget and live-review headroom apply throughout.

Use `lantern reviewer-pilot status` for counts and spending, or `lantern reviewer-pilot results` for private, per-case decisions, reasons, costs, and timings. Agreement covers the proposed action and each concern's disposition and tool scope. Evaluate differing reasons and proposed operator questions against Mike's recorded permissions. To close the pilot early, run `lantern reviewer-pilot stop`. A call already in flight can finish settling. The collector exits when collection ends.

## Administrator sign-in

The installer provides `lantern-admin-web.service` on loopback port 8766. To opt in, create `/etc/lantern/admin-web.env` as root with mode 0600. Set `LANTERN_PUBLIC_ORIGIN=https://lantern.example.net`, replacing the example with the exact HTTPS origin you own. Set the password in an interactive terminal, then start the service.

```sh
sudo -u lantern -H /usr/local/bin/lantern web-admin set-password
sudo systemctl enable --now lantern-admin-web.service
```

Only the operator's shell can set a password. The host stores a salted password digest and digests of expiring session tokens in the private state file `web-auth.sqlite`. The password and tokens stay outside the conversation and the coding workspace. Eight unsuccessful password checks in five minutes temporarily limit sign-in. To revoke every administrator session while keeping the password, run:

```sh
sudo -u lantern -H /usr/local/bin/lantern web-admin revoke-sessions
```

Once the hostname resolves to the instance, add its own site to the existing HTTPS proxy. This example uses Caddy. Preserve the browser's Host and Origin headers. Keep both backend ports bound to loopback. The SSH room on port 8765 uses a separate credential.

```caddyfile
lantern.example.net {
    reverse_proxy 127.0.0.1:8766
}
```

On the shared Lightsail host, use the existing Caddy configuration lock and validation/reload procedure. Verify certificate trust, a signed-out visit, and access to the existing room after signing in. Restart the administrator service after changing its configured origin. A normal restart preserves sessions. The operator can recover access by setting a fresh password over SSH.

## Chat

From the laptop's normal terminal:

```sh
ssh -t lightsail "sudo -iu lantern lantern"
```

Within an existing `lantern` shell, run `lantern`. Use `lantern status`, `lantern history`, and `lantern usage` for one-shot checks. The system unit is enabled across boots. Worker logs are available to the administrator with `sudo journalctl -u lantern-chat.service`.

The unit passes `--heartbeat-minutes 60`. At startup the worker uses its active configuration to synchronize the native tool definitions in the private room's header and its same-thread compaction checkpoints. This covers additions, removals and revised schemas. The worker holds its exclusive lock and takes a private backup before changing the saved metadata and history offsets. Current migrations store the original rollout and the target registry under `operator-backups/room-tools-v2/`. The pinned persona, messages and thread ID survive the update. A later restart uses the saved heartbeat cadence. Inspect it with `lantern schedule`. The worker reads and writes project files under `workspace/files/` through supervised tools.

## Browser

Use the [SSH command in the project README](../README.md#talk-to-lantern-on-lightsail) on the laptop. The local and remote ports for the SSH room are both 8765. Its web service listens on host loopback, issues a fresh key at startup, and saves it in the owner-only `web-key` file in Lantern's private state. The operator link passes the key in the URL fragment. The browser clears it from the address bar and authenticates API requests from the tab. To rotate tunnel access, restart `lantern-web.service` and get a fresh link. Starting or stopping the web service does not interrupt the model worker.

## Coding environment

For a PDF viewer upgrade, install the coding broker and image first, then update the chat worker with `deploy/install.sh --worker-only FULL_GIT_REVISION` at an idle boundary. The existing browser connection stays available.

For a new installation, make the seed with `git bundle create lantern.bundle --branches --tags` from the reviewed repository. The coding installer builds a dedicated Python and Git image. Only the broker can reach Docker. The worker sends approved commands over `/run/lantern-code.sock`. Docker uses a pinned local image ID and starts a fresh unprivileged container with its own process namespace, offline networking, a read-only root and a writable coding volume. Each command gets 30 seconds, half a CPU, 256 MiB of RAM, 64 processes and 16 KiB of output. The 256 MiB ext4 volume is backed by `/var/lib/lanterncode/work.ext4` and mounts across boots.

The full editable clone is `/work/lantern`, also available as `~/src/lantern` inside the container. The host operator's checkout is `~lantern/src/lantern`. The installer seeds the coding clone once. Subsequent service updates preserve branches and uncommitted work. The worker loads the root-owned release under `/opt/lantern/worker-current`, as does the password-authenticated browser. The SSH-link browser loads its release under `/opt/lantern/current`.

The same broker accepts a supervised one-page PDF rendering request. Poppler runs in a separate disposable, offline container with one read-only mount. An absolute `/work/` PDF comes from the coding volume. For `heritage/lmstudio-chat.pdf`, the mount contains only the operator-provisioned PDF. The installer verifies its digest against the private page-text companion and stages it under the root-only `/var/lib/lanterncode` directory. Pages fit within 1,600 pixels per side and 1 MiB of PNG data. Rerun the coding installer after replacing or removing the heritage archive. A standalone coding deployment can operate with workspace PDFs alone.

From the host's normal `ec2-user` shell, `sudo ls -la /var/lib/lanterncode/work/files` lists Lantern's persistent `/work`. Append `/lantern` to browse its checkout. For an interactive read-only view as the workspace owner, start a separate container on that volume.

```sh
sudo docker run --rm -it --pull=never --network none --cap-drop ALL \
  --security-opt no-new-privileges --read-only \
  -v /var/lib/lanterncode/work/files:/work:ro \
  -w /work/lantern lantern-code:local
```

Use `exit` to close the inspection shell. The volume remains available between Lantern's own coding commands.

The first worker start with coding enabled backs up the existing native room in `operator-backups/coding-workspace-v1/` before adding the tool. The worker, browser and broker can be inspected separately with `systemctl status lantern-chat lantern-web lantern-code.socket lantern-code.service`. For a coding-only outage, recover the mount and broker before retrying. Check for completed edits if a command lost its response.

## Approved file imports

Install the matching coding image and broker using `sudo bash deploy/install-coding.sh PATH_TO_SEED_BUNDLE FULL_GIT_REVISION`, then activate the matching worker at an idle boundary. On an already seeded host the installer requires an existing bundle path and preserves the editable checkout. Restart `lantern-admin-web.service` to load the room controls from the worker release. Use the full installer to update the optional SSH-link web room as well.

The coding installer enables file imports for the worker and provisions a private staging directory. The room stores its decisions in `file-imports.sqlite` under the existing private state directory. The downloader accepts only the exact public HTTPS sources approved for a named file. It checks every redirect and presents a new mirror in the room for a separate decision. The offline broker verifies the full digest and delivers a regular file without replacing an existing entry. Identical bytes at the same destination can satisfy an interrupted delivery.

A request can cover up to 12 named files. Each file can list up to three redirect URLs. The download limit is 16 MiB per file, with 64 MiB total staging capacity. The UTF-8 data formats are plain text, CSV, TSV and JSON (2 MiB for JSON). PDF documents are imported as binary files. Use `view_pdf_page` with the path from the receipt to render individual pages offline. Tar, gzip-compressed tar and numbered archive parts can be stored for inspection. The import operation never extracts archive members or executes their contents. File verification checks the full expected SHA-256 when the grant specifies one. Otherwise the receipt records the digest calculated on arrival. A new source, another use of the data, and public redistribution each have their own authority.

For a first check, select **Use NASA’s WMAP spectrum** in the room, review the source and filename, and allow it once. Ask Lantern to locate the grant with `file_import_status`, import `wmap-spectrum.txt`, and find the multipole bin with the highest average temperature power. The sample pin was measured from the NASA download. [NASA product and documentation](https://lambda.gsfc.nasa.gov/product/wmap/dr5/pow_tt_spec_get.html)

## Approved repository clones

Use the matching coding installer, worker and signed-in room as described under **Approved file imports**. The coding installer provisions the clone staging directory and enables `LANTERN_REPO_CLONES` on the worker. The room records requests, decisions and delivery receipts in `repository-clones.sqlite` in private state.

Lantern supplies a public GitHub `owner/repo` or its HTTPS URL and a purpose. The host anonymously resolves the requested branch (by default, the repository's default branch) and records the repository's numeric GitHub identity and commit. Mike sees the proposal in **Repository clones**. One-time permission covers the displayed commit. Branch permission starts there and covers subsequent tips of that branch in the same repository. Each update is pinned before review. Receipts record the previous and delivered revisions. Mike can deny a request, revoke further deliveries or reconsider a closed request.

The transfer is limited to a 32 MiB Git pack, 64 MiB of checked-out file contents and 8,192 tree entries. The offline coding service creates an editable, depth-one checkout at `/work/repos/<owner>/<repo>/<commit>`. Existing work at earlier revisions is preserved. File content is materialized without invoking project programs or Git filters. Submodule directories stay empty, Git LFS pointers stay as text, and symlinks are represented by their link text. Builds or other commands use the existing supervised coding tool.

For an acceptance check, let Lantern request a repository he wants to study. The example `lsernau/grh-lean` needs no special configuration. Inspect the reason, branch and displayed commit in the room, choose one-time approval, then have Lantern use the grant ID. Compare the receipt's commit to `git rev-parse HEAD` in the delivered directory and ask him to identify one tracked file he can read there.

## GitHub sync and draft pull requests

From the reviewed source archive on Lightsail, run `sudo bash deploy/install-github.sh FULL_GIT_REVISION` after provisioning the coding environment. Then activate the matching chat worker with `sudo bash deploy/install.sh --worker-only FULL_GIT_REVISION`. This enables the tools on the shared conversation, including after resume and compaction.

Sync and preview use anonymous read access. For publication, create a fine-grained GitHub token owned by `mikepfrank`. Choose **Only select repositories**, select **lantern-agent**, and give **Contents** and **Pull requests** read and write access. On Lightsail run `sudo /usr/local/sbin/lantern-github-auth` and paste the token at its hidden prompt. Reuse the command to rotate it. Each submission reads the current credential. [GitHub token setup](https://github.com/settings/personal-access-tokens/new?name=Lantern+draft+proposals&target_name=mikepfrank&contents=write&pull_requests=write)

1. Ask Lantern to run `github_sync`. The host fetches only this project's public `main`. A fixed helper imports it as `upstream/main` inside the offline coding container. A clean checkout can fast-forward. If the result reports local edits, divergence, or an operation in progress, Lantern can inspect and integrate locally with `coding_run`.
2. Lantern makes a local commit and passes its full SHA to `github_preview`. The host builds a private preview against the known upstream commit. The preview shows the public destination, title, body, files, and exact patch. For an initial test, choose a small documentation correction. Review that the proposed text is suitable for the public repository.
3. Lantern can copy the preview's 12-character `proposal_id_prefix` into the `proposal_id` argument of `github_open_draft`. If several frozen previews share the prefix, use more characters from the full ID. Review and publication use the full verified proposal ID. The publisher creates a dedicated proposal branch with a single commit and opens a draft against `main`. Prior workspace commit history, uncommitted files, and private notes are outside the published patch. Reuse the same ID after an interrupted submission. Mike and the maintainer handle merge and deployment separately.

The first version handles text patches up to 12 KiB and 16 regular source, test, deployment or documentation files. A new proposal based on stale upstream needs a fresh sync and preview. The network-facing service runs as `lantern-git`; a separate offline helper runs two fixed Git operations against the coding volume. Only the publisher receives the GitHub credential. Frozen previews stay in the publisher's private `/var/lib/lantern-github` directory. Check `systemctl status lantern-github.socket lantern-git-work.socket` for availability.
