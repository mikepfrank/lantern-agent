# Lantern project handoff

## Current state

**Operator check, November 18, 2026 (US Central).** Mike inspected the private state directory on Lightsail and chatted with Lantern in the existing room after the persona and heritage update. He reported that Lantern seemed more confident about its origins and its role. This completes the manual check of the updated room. Continue using the same conversation. The next planned milestone is persistent alarms and adjustable heartbeats under the existing daily budget.

Milestone 3 adds a private, persistent SSH terminal chat. From Mike's laptop, run `ssh -t lightsail "sudo -iu lantern lantern"`. The operator can enter more messages during a reply, detach with `/quit`, and return to the same conversation. The host queues work in `chat.sqlite` alongside the existing persona, supervision and usage databases. There is one operator conversation and one worker. An idle worker makes no model requests. Heartbeats and alarms are a later milestone.

Native Codex accepts active input with `turn/steer` and may replace a response already in flight. Both requests can incur charges. A confirmed rejection leaves the follow-up queued for the next turn. Ambiguous delivery after a crash produces an interruption notice with the affected message IDs. Check for any completed tool effects before explicitly resubmitting. The worker never automatically repeats an uncertain action. Operator messages, deliberate interim notifications and final replies remain in the terminal history.

The [pilot deployment guide](deploy/README.md) covers the systemd unit, release selection, private state, CLI and rollback. The shell, general-purpose workspace, GitHub proposal tool and autonomous wakeups still belong to later milestones. The pilot offers supervised chess analysis, consultation with Opus, private notices to the operator, and access to the founding archive.

### Lightsail pilot

Use `readlink -f /opt/lantern/current` to identify the active release. The root-owned service `lantern-chat.service` runs as `lantern` and starts on boot. The operator can run `lantern` in that account. The installed Codex is 0.169.0 and the worker defaults to OpenRouter throughput routing. An interactive turn has ten minutes for generation, tools and review, and each Qwen request allows 8,192 output tokens. The shared UTC budgets are $10 for Qwen and $5 for Luna and Opus combined.

The Lightsail launcher selects `--private-lightsail-cli`. On each model request for this channel, the harness identifies Michael P. Frank as Lantern's developer, the host's administrator, and its only interactive human user. It supplies the same channel information to Luna and Opus. This applies when resuming an existing room independently of its pinned persona. Before adding any other human users or input sources, give each channel its own operator context and separate its conversation history from Mike's private room.

The pilot uses the host account's persistent state at `~lantern/.local/state/lantern`. A full writable Git checkout lives in `~lantern/src/lantern` outside the worker's filesystem view. Its fetch source is the operator-owned `/opt/lantern/source/lantern.bundle`. The writable in-container checkout, GitHub remote and PR workflow are future work. The chess and GPT-2 services run independently of the Lantern deployment.

For routine inspection, log in as `lantern` and open `~/.local/state/lantern`. The running pilot uses a systemd mount namespace. To inspect its exact filesystem view, an administrator can enter the main PID's mount namespace with `nsenter` and use `setpriv` to drop to the `lantern` user and group before opening a shell. In that view the state directory contains `workspace/` and the read-only `heritage/` mount. Lantern itself reads the archive through the supervised tool.

### Private persona and heritage archive

Mike supplied a revised `Lantern.md` and three exports of the founding LM Studio conversation between Mike and Qwen on his MacBook. The local project keeps them under the ignored `heritage/` directory. The private Lightsail launcher uses `~lantern/.local/state/lantern/heritage/`, outside both Git checkouts and the installed application bundle. Provision or restore these files over a private channel. Keep the originals, extracts, screenshots and quotations out of GitHub issues, PRs, commits and public research queries.

The private CLI uses `Lantern.md` as the default persona for new threads. The active operator room also uses that revision. Other saved threads retain their pinned personas.

Codex 0.169 persists its tool registry and the original developer persona in the native thread history. For the existing room, release `288fcef` introduced the reader and replaced the exact old developer persona in the saved thread. An offline rehearsal on a private copy verified the outgoing persona, tool list and original thread ID. The pre-change room files are under the private state directory in `operator-backups/heritage-288fcef/`. Restoring that snapshot after subsequent chat would discard intervening work; use a copy for inspection.

Lantern can call the supervised `read_heritage` tool to read passages on demand. The portable edition is `lmstudio-chat.md`. The verbatim export `lmstudio-chat.txt` preserves LM Studio's role markers on disk and the reader escapes their delimiters. The illustrated original is `lmstudio-chat.pdf`. A private, digest-matched `lmstudio-chat.pdf-pages.json` provides text by page. The current reader returns quotations to the text model; the images remain in the original PDF.

The commissioning exchanges remain visible in `/history`. The first delivered an interim greeting, then Opus objected to disclosing a test receipt identifier and the Qwen response hit its old 4,096-token limit. In the second exchange, both test clients disconnected after changing a candle question to ask about an LED. The final answer used the new example. Codex had already received the replacement answer while the obsolete generation was still reporting costs. The current worker releases the validated answer and continues settling the specific superseded request in the background. An acknowledged steer alone never releases a budget reservation.

After restarting the final release, the private room retained its thread ID. A live question asking for any planet was steered to ask for the largest one. Both operator messages belonged to the same native turn, and a separate client read the answer “Jupiter” after 20.9 seconds. All seven OpenRouter charges across the three tests settled for a total of $0.024306155. Three Luna calls and one Opus call added an estimated $0.02074115. Use ordinary operator traffic to estimate typical review rates and latency. All 65 automated tests passed on Amazon Linux, including native Codex steering, POSIX terminal behavior, process-kill recovery and separate validation of billing after reply publication.

## TODO

- Polish the SSH terminal as a fallback for times when the web UI is unavailable. Revisit the interaction in a real tmux session when working on the web interface.
- Once the web interface is available, let Mike attach images and let Lantern inspect them natively. Verify that the selected Qwen endpoint receives the image alongside Mike's accompanying text.
- Give Lantern a supervised PDF workflow in the live agent, including on-demand text extraction and rendering a selected page for visual inspection of diagrams and illustrations through the same image-input path.

## Milestone 2

Milestone 2 adds mandatory review to the on-demand CLI. GPT-6 Luna screens each tool call and the first visible context window plus every 12th request thereafter in that thread. A flagged proposal goes to the specific legacy Claude 3 Opus model with Luna's summary and evidence and Lantern's rationale. A denied call returns the detailed judgment. Calls to tools outside the registry stay blocked even when a reviewer considers the idea benign. Reviewers and research run in the operator process, outside the Codex environment.

The local Windows regression uses native Codex and simulated vendors to test the full denial-to-Qwen path, voluntary advice, a later allowed tool call, persona, and thread resumption. The complete suite passed 44 tests. On Lightsail, real Luna cleared chess analysis, and a pre-milestone-2 thread also resumed with supervised chess. In a separate synthetic case, real Luna referred a proposed publication of private annotations without consent, and legacy Opus independently denied it. The publication was a test description. The harness exposes chess and consultation.

An early live advisory was held when the supervisor returned no usable result. On retry the Opus guidance reached Qwen. An advisory may now request one formatting correction after an unreadable XML response. Invalid adjudications hold the action. The consultation tool returns the host's record of searches so Lantern can distinguish completed research from a request to look something up. Use `python -m lantern usage` for the separate supervisor cost/latency breakdown and review outcomes.

The milestone 2 calibration used a temporary $0.50 supervisory ceiling. Estimated direct API charges totaled $0.25214990 (8 Luna calls, $0.00205490, and 12 Opus calls, $0.250095). No supervisor reservations remained. The six Luna tool screens averaged 3.33 seconds. Eleven of the Opus calls deliberately tested consultations, including research rounds and a held response. This workload does not estimate normal escalation frequency. Both research services returned live results from Lightsail. In a final advisory, the host observed two Crossref searches with three hits each. Mike selected a shared $5 per UTC day for Luna and Opus, independent of Qwen's $10 daily OpenRouter allowance.

The milestone 2 tests on the shared Lightsail host ran on demand. Further calibration will use the normal usage ledgers.

## Milestone 1

Milestone 1 passed on 2026-11-18 (US Central) with [Qwen's own Lantern statement](lantern/personas/lantern-v1.md) as the default for new threads. The repository preserves the attachment byte for byte. The harness reads it as UTF-8 text, normalizes line endings, and pins that text to each thread. The GitHub remote, persistent service and production container are future milestones.

### What was verified

- Windows passed 15 automated tests using Python 3.12, including an offline test that launched two independent native Codex CLI 0.169.0-alpha.11.1 processes. This test checks the full bundled persona in developer content before user input on every request, the tool call and result, the resumed history, both routing choices, and the ledger.
- With the bundled persona, the Lightsail smoke test inspected each outbound request before forwarding it to OpenRouter. The complete statement preceded user input on all three requests. Qwen explained the engine's `Qg7#` and recalled it and the ticket after a restart. A fourth request resumed the same thread through Darkbloom and verified that Lantern understood the ticket came from Codex's saved history and that the current tools cannot write a notes file. Total reported cost for this persona verification was $0.00670761.
- On Lightsail the test ran as `lantern` using Python 3.12 and a temporary copy of the verified Codex 0.169.0 Linux bundle. The account's OpenRouter key stayed on the host. Qwen used the inherited chess engine, explained `Qg7#`, and recalled both the mate and a fresh ticket after restarting Codex. In the final run, OpenRouter selected ModelRun for both the throughput and price requests. These are routing preferences and can select the same provider.
- During the earlier placeholder tests, the final live run cost $0.00295135 across three requests. Total reported spend across those two successful runs was $0.00764615. Two earlier HTTP 404s retained conservative reservations totaling $2.080768 against the 2026-11-19 UTC allowance in the prototype ledger. The corrected gateway recognizes the specific pre-generation rejection and releases its hold for new occurrences.
- OpenRouter returned “No endpoints found that can handle the requested parameters” with `require_parameters=true`. Omitting that optional filter resolved the problem while retaining the pinned model, allowlisted tool and price ceilings.
- The operator CLI and the tests are described in [README.md](README.md). The live prototype state, including the thread store and budget SQLite file, is private to `lantern` under its default state directory. No chess or GPT-2 service changes were required.

Lantern's own shell access, general web search, scheduled wakeups and GitHub PR submission are later milestones.

## Lantern's copy of its harness

Before deployment, give the unprivileged Lantern user a **full, writable Git clone** in its persistent container home at `~/src/lantern`. Include branches, tags, and their history. Lantern can inspect the harness, change it, run tests against its own copy, and make commits. Preserve its working tree, including uncommitted changes, across container replacement.

Put the live release in an operator-owned directory such as `/opt/lantern`, exposed read-only inside the container. Start the installed service from this release using trusted configuration and import paths. Store mutable runtime data separately. Tests and experiments launched in `~/src/lantern` can exercise proposed code without changing the code or policy loaded by the live service.

For provisioning, the host can make a Git bundle from a current full mirror of the canonical repository with `git bundle create lantern.bundle --all`. Present that bundle read-only for initial setup. Run these commands as the container user with the credential-free canonical URL in `LANTERN_REPO_URL`.

```sh
mkdir -p "$HOME/src"
git clone /run/provision/lantern.bundle "$HOME/src/lantern"
git -C "$HOME/src/lantern" remote set-url origin "$LANTERN_REPO_URL"
```

Clone once when the persistent checkout is absent. On subsequent starts reuse it. Future upstream changes should be available to fetch without overwriting Lantern's work.

Lantern should be able to submit a pull request through a reviewed tool call. The host-side tool uses a project-scoped GitHub identity to publish a proposal branch and open a PR against the canonical repository. Keep credentials outside the container and outside any process that runs Lantern's Git hooks, configuration, or tests. Run proposal tests without deployment credentials. Mike and the local project maintainer review proposed changes here before acceptance. Deploy from an explicitly selected, reviewed commit, independently of the writable checkout.

### Pre-deployment acceptance test

1. Compare the checkout with the canonical revision, branches, and tags. Verify that an older commit is readable, and that a write to the live release from the agent account is denied.
2. As Lantern, edit a tracked file, run a test, and commit on a proposal branch. Leave a second edit uncommitted and verify both edits survive container replacement.
3. Use the submission tool to open a test PR. Check that its base and diff match the proposal, its commits are attributed to Lantern, and the canonical branch and running revision still match their pre-test values.

A local Git smoke test on 2026-11-18 exercised the mirror, bundle, and clone steps with two commits, a second branch, and a tag. A proposal commit in the clone left the upstream revision intact. Container isolation, volume persistence, and the GitHub submission path still need the deployment tests above.

## Other agreed design decisions

- The terminal chat comes first. Messages and alarms should survive client disconnects. On idle heartbeats Lantern may pursue interests it chooses and adjust its cadence.
- Reserve OpenRouter spend before each request against a $10 UTC daily limit. Track OpenAI and Anthropic supervision costs separately so Mike can calibrate their allowance.
- Luna reviews proposed tool calls and periodically samples full context windows. Escalations go to Claude 3 Opus through Mike's legacy Anthropic API access. Supply the current date, deployment context, Mike's reason for entrusting Opus with review, concise evidence, Lantern's explanation, and read-only web research. Return detailed denials to Lantern and support voluntary consultation.
- Keep Lantern's versioned persona separate from the operating instructions. Reuse the Arcturus chess tools and test Qwen's tool calls, routing choices, and session resumption before moving on to independent wakeups and the later web UI.
