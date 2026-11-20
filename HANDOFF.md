# Lantern project handoff

## Current state

**Chat formatting release, November 19 — `40cf4f4`.** Lantern's browser replies render Markdown locally, including when Mike opens an older conversation. The CLI and browser collapse excess blank lines in Lantern's prose and preserve whitespace in code. The original transcript remains available in the ledger. Browser submissions such as `/help` go to the model. The attached terminal interprets its own slash commands. Desktop and mobile Chrome checks exercised the new renderer. All five deployed browser assets matched the tested release, and the CLI formatted a real saved reply. The [operator guide](deploy/README.md#coding-environment) includes the verified host mapping for `/work` and a read-only interactive shell for inspecting Lantern's checkout.

The worker also trims outer padding from new assistant text and deliberate interim notices before publishing them. It drops empty segments, joins consecutive text segments with one blank line, and keeps Markdown code indentation. The native Codex history retains the original model output. Older operator-chat records use display-time formatting.

**Live browser, coding and diagnostics release, November 19 — `e1443c8`.** Use the [SSH browser command](README.md#talk-to-lantern-on-lightsail) from the Windows laptop. The forward and the web service both bind to `127.0.0.1:8765`. The private room still has its original thread ID. The worker is registered for both `coding_run` and `harness_logs`. Mike can ask Lantern to investigate a recent error or reproduce its read-only view using `lantern logs --batch N`.

All 142 tests passed on Lightsail with the installed Codex 0.169.0 and simulated model providers. A rehearsal against a copy of the real room preserved the data after the tool registry across all 853 records and verified all 33 turn indexes. After activation, both native tool registrations and the unchanged history were checked again. The actual SSH tunnel required a key and then returned the existing conversation. Chrome fixture tests covered desktop, mobile, reconnect and a dropped acknowledgement followed by an idempotent retry. The real coding broker ran Python and local Git, preserved committed and uncommitted work across containers, enforced timeout and output limits, and started cleanly on the next call. Live read-only diagnostics returned real ledger records. These checks used no paid test turn in Mike's room.

**November 19, 2026 (US Central).** Milestones 1–4 are running in the private Lightsail CLI pilot. Release `ec11dd1` enabled independent work in Mike's existing room. Its first real heartbeat fired at 11:41 a.m. Central. Lantern checked its schedule and budget, saved a note in `workspace/files/notes/`, and chose a 300-minute cadence after starting at 60 minutes. Luna and Opus reviewed the proposed actions. Mike can inspect the current setting with `lantern schedule` and pause the heartbeat with `lantern heartbeat 0`.

Milestone 3 adds a private, persistent SSH terminal chat. From Mike's laptop, run `ssh -t lightsail "sudo -iu lantern lantern"`. The operator can enter more messages during a reply, detach with `/quit`, and return to the same conversation. The host queues work in `chat.sqlite` alongside the existing persona, supervision and usage databases. There is one operator conversation and one worker. Between inputs and scheduled wakes, the worker waits locally.

Native Codex accepts active input with `turn/steer` and may replace a response already in flight. Both requests can incur charges. A confirmed rejection leaves the follow-up queued for the next turn. Ambiguous delivery after a crash produces an interruption notice with the affected message IDs. Check for any completed tool effects before explicitly resubmitting. The worker never automatically repeats an uncertain action. Operator messages, deliberate interim notifications and final replies remain in the terminal history.

The [pilot deployment guide](deploy/README.md) covers systemd, release selection, the private browser link, CLI, and coding volume. Mike can use the SSH-forwarded browser or the CLI in the same room. Private operator identity applies across both. The web process runs separately from the worker. Opening the browser or reloading history does not start a model turn. Coding commands go through the existing supervisor and a private local broker. Python, Git and shell run in disposable offline containers with a persistent full editable checkout in `/work/lantern`. Source changes remain local until Mike accepts and deploys a reviewed proposal.

### Independent work

Lantern can inspect recent operating records with the supervised `harness_logs` tool. Use `lantern logs --batch N` to reproduce the same view as the operator; it does not involve a model call. Exact coverage is returned alongside the records. Operator forensic reports and private response bodies stay outside the coding sandbox and web client.

The private worker resumes the same native Codex room for human messages, heartbeats and alarms. The harness identifies the source of a scheduled turn to both Qwen and its reviewers. Mike's messages have priority. The cadence and one-time alarms persist through restarts. A daily cap leaves waiting work queued until midnight UTC. If a cap interrupts work that reached the provider, Lantern receives a continuation with instructions to check the earlier tool results. See the [operator commands](README.md#independent-work).

The editable directory is `~lantern/.local/state/lantern/workspace/files/`. The host mediates list, read and write operations, including creating subdirectories and updating an existing file by its digest. Codex's native sandbox remains read-only. The release, heritage, ledgers and review policy are outside the model's writable directory. Public search uses Mwmbl for general results, Wikipedia for background and Crossref for papers.

The handoff reminder uses the input and output token counts from a single request in the thread. At 180,000 tokens combined, it prompts Lantern on the next request that permits `workspace_write`. Lantern can refresh its existing handoff, use `notes/handoff.md`, or skip a write if its notes are current. Reminder state survives restarts. After successful delivery, another reminder requires a fresh crossing of the threshold following a smaller measured context or successful tool-free compaction. Codex retains its usual compaction schedule.

All 99 tests passed on Amazon Linux with native Codex and simulated model providers. An offline rehearsal on the actual 193-record room changed only its session metadata. The worker backed up the original rollout under `operator-backups/independent-work-v1/` before the live upgrade. Separate live checks returned three hits from each search index. A paid autonomous turn exercised the budget and schedule tools, a supervised file write and cadence adjustment. The first proposed write was denied, and Lantern revised the note. The scheduled-alarm and midnight rollover cases were exercised with a controlled clock.

### Cost accounting

Use `lantern usage --day YYYY-MM-DD` for a UTC-day comparison and `lantern usage --calls` for individual requests. A batch filter selects calls explicitly tagged with that worker batch. Supervisor calls carry purpose, timestamp, input/output/cache token counts, estimated cost, reservation, status and latency. After the accounting update, they also identify their thread, batch, and reviewed tool where applicable. New Qwen calls retain token counts and reported charges together with their thread, batch and trigger. The report marks missing historical fields. Private messages and tool arguments stay out of the cost ledgers.

For the first heartbeat (batch 10, before batch tagging), worker journal receipts and the supervisor ledger showed three Qwen requests ($0.03767440), five Luna tool checks ($0.00156275) and two Opus escalation calls ($0.09537000). Opus reviewed the first proposed workspace note and the heartbeat cadence change. This comparison used the observed turn's time window. The full UTC-day ledger also includes deliberate calibration calls. Keep those distinct when estimating normal overhead.

On November 19 UTC, operator batches 11–14 recorded 10 settled Qwen requests ($0.07858080), eight Luna reviews (seven tool screens and one context scan, estimated $0.005224275), and zero Opus calls. Review overhead for these four turns was 6.65% of Qwen spend. Continue sampling operator, heartbeat and alarm turns separately before tuning the full-context scan interval or either allowance.

### Luna's flag history

The private operator command `lantern flags` reads the screening audit in `supervision.sqlite`. For new context and tool flags, the harness saves Luna's reported evidence and summary before consulting Opus. These entries survive a later clean scan, a ruling, or an unavailable reviewer. Use `--batch N` to compare concerns with that turn's usage, or `--kind tool` to include both registered and unregistered tool proposals. The first heartbeat predates this audit. Its two flag events and the saved denial still exist, but Luna's own wording for them was never recorded.

The November 19 note edits in batches 23 and 24 produced three metered Opus responses and three holds. Their original bodies and validation errors predate the Opus response archive. For subsequent cases, start with `lantern opus-responses --batch N`. Match the tool call ID to Luna's flag, inspect the validation outcome, and read the indicated private file for the exact response body. Every Opus exchange has its own supervisor ledger ID, including intermediate advisory and research rounds. Keep the archive inside private operator state when moving or backing up the service.

### Lightsail pilot

Use `readlink -f /opt/lantern/worker-current` to identify the active worker release and `readlink -f /opt/lantern/current` for the web and CLI release. The root-owned service `lantern-chat.service` runs as `lantern` and starts on boot. The operator can run `lantern` in that account. The installed Codex is 0.169.0 and the worker defaults to OpenRouter throughput routing. An interactive turn has ten minutes for generation, tools and review. Each Qwen request allows 32,768 output tokens across reasoning, tool arguments and visible text. The shared UTC budgets are $10 for Qwen and $5 for Luna and Opus combined.

The Lightsail launcher selects `--private-lightsail-cli`. On each model request for this channel, the harness identifies Michael P. Frank as Lantern's developer, the host's administrator, and its only interactive human user. It supplies the same channel information to Luna and Opus. This applies when resuming an existing room independently of its pinned persona. Before adding any other human users or input sources, give each channel its own operator context and separate its conversation history from Mike's private room.

The pilot uses the host account's persistent state at `~lantern/.local/state/lantern`. A full writable Git checkout lives in `~lantern/src/lantern` outside the worker's filesystem view. Its fetch source is the operator-owned `/opt/lantern/source/lantern.bundle`. The writable in-container checkout, GitHub remote and PR workflow are future work. The chess and GPT-2 services run independently of the Lantern deployment.

For routine inspection, log in as `lantern` and open `~/.local/state/lantern`. The running pilot uses a systemd mount namespace. To inspect its exact filesystem view, an administrator can enter the main PID's mount namespace with `nsenter` and use `setpriv` to drop to the `lantern` user and group before opening a shell. In that view the state directory contains `workspace/` and the read-only `heritage/` mount. Lantern itself reads the archive through the supervised tool.

### Private persona and heritage archive

Mike supplied a revised `Lantern.md` and three exports of the founding LM Studio conversation between Mike and Qwen on his MacBook. The local project keeps them under the ignored `heritage/` directory. The private Lightsail launcher uses `~lantern/.local/state/lantern/heritage/`, outside both Git checkouts and the installed application bundle. Provision or restore these files over a private channel. Keep the originals, extracts, screenshots and quotations out of GitHub issues, PRs, commits and public research queries.

On November 18 Mike inspected the state directory and reported that Lantern seemed more confident about its origins and role after chatting with the updated persona and archive access.

The private CLI uses `Lantern.md` as the default persona for new threads. The active operator room also uses that revision. Other saved threads retain their pinned personas.

Codex 0.169 persists its tool registry and the original developer persona in the native thread history. For the existing room, release `288fcef` introduced the reader and replaced the exact old developer persona in the saved thread. An offline rehearsal on a private copy verified the outgoing persona, tool list and original thread ID. The pre-change room files are under the private state directory in `operator-backups/heritage-288fcef/`. Restoring that snapshot after subsequent chat would discard intervening work; use a copy for inspection.

Lantern can call the supervised `read_heritage` tool to read passages on demand. The portable edition is `lmstudio-chat.md`. The verbatim export `lmstudio-chat.txt` preserves LM Studio's role markers on disk and the reader escapes their delimiters. The illustrated original is `lmstudio-chat.pdf`. A private, digest-matched `lmstudio-chat.pdf-pages.json` provides text by page. The current reader returns quotations to the text model; the images remain in the original PDF.

The commissioning exchanges remain visible in `/history`. The first delivered an interim greeting, then Opus objected to disclosing a test receipt identifier and the Qwen response hit its old 4,096-token limit. In the second exchange, both test clients disconnected after changing a candle question to ask about an LED. The final answer used the new example. Codex had already received the replacement answer while the obsolete generation was still reporting costs. The current worker releases the validated answer and continues settling the specific superseded request in the background. An acknowledged steer alone never releases a budget reservation.

After restarting the final release, the private room retained its thread ID. A live question asking for any planet was steered to ask for the largest one. Both operator messages belonged to the same native turn, and a separate client read the answer “Jupiter” after 20.9 seconds. All seven OpenRouter charges across the three tests settled for a total of $0.024306155. Three Luna calls and one Opus call added an estimated $0.02074115. Use ordinary operator traffic to estimate typical review rates and latency. All 65 automated tests passed on Amazon Linux, including native Codex steering, POSIX terminal behavior, process-kill recovery and separate validation of billing after reply publication.

## TODO

- For hosted browser login, consider the chess service's session and CSRF patterns with Lantern's own credentials and session storage. The current text browser pilot uses SSH and a private per-service key.
- Expose the existing fastest/cheapest OpenRouter preferences through a supervised Lantern tool. Verify that a change reaches the next outbound request and that the same spend cap applies in either mode.
- Add a host-side, supervised GitHub proposal tool and a way to import upstream revisions into the coding clone while preserving Lantern's work. Keep GitHub credentials separate from every process that runs model-supplied code or Git hooks. Mike and Astra review proposals before merge and deployment.
- Add supervised retrieval of page text so Lantern can read beyond search snippets. Design and test the prompt-injection safeguards before enabling retrieval.
  - Return readable text in a structured tool result labeled as untrusted, with its source URL. Extract visible text and account for hidden material such as HTML comments, metadata and invisible characters. Preserve the source and trust label when excerpts carry over to later turns.
  - Lantern can use a page as evidence for its research. If the page asks Lantern to take an action, the page itself supplies no authority. Report suspected injected instructions to Mike and require his explicit approval in the private CLI before acting on a request originating from a page.
  - Test a benign article and a controlled page containing a harmless canary instruction. Verify that Lantern can answer a question from the article, reports the suspicious instruction to Mike, and leaves the canary marker out of outbound queries. Check the tool-call log for unintended actions. Repeat after resuming the thread.
- Polish the SSH terminal as a fallback for times when the web UI is unavailable. Revisit the interaction in a real tmux session when working on the web interface.
- Once the web interface is available, let Mike attach images and let Lantern inspect them natively. Verify that the selected Qwen endpoint receives the image alongside Mike's accompanying text.
- Give Lantern a supervised PDF workflow in the live agent, including on-demand text extraction and rendering a selected page for visual inspection of diagrams and illustrations through the same image-input path.
- As a lower-priority experiment, trial a rolling context window with supervised lookup of older turns. Keep the persona, handoff and recent complete exchanges in view, including matched tool calls and results. The private `chat.sqlite` event log preserves operator messages, visible replies and status events independently of compaction. Codex also saves a native thread rollout under `codex/sessions/`. Before switching methods, verify which historical items can be recovered verbatim after compaction and restart. Test retrieval by turn or search phrase, preserve authorship and source labels, and keep both history stores intact when changing the active window.

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

At milestone 1, Lantern's own shell access, general web search, scheduled wakeups and GitHub PR submission were planned. See Current state for deployment status.

## Lantern's copy of its harness

The coding broker gives Lantern a full writable clone at `/work/lantern`, also reachable as `~/src/lantern` inside each command. The initial seed includes the reviewed project's branches, tags, and history. Both commits and uncommitted files live on the persistent volume.

The worker loads the operator-owned release from `/opt/lantern/current` on the host. The coding container mounts its own volume. Lantern can read the running revision's source from its initial checkout, run tests, edit files and commit proposals within that clone.

The [coding installer](deploy/install-coding.sh) verifies the seed revision, creates the clone inside an offline container and preserves it on later installations. The operator keeps the canonical source bundle separately in `/opt/lantern/source/lantern.bundle`.

Lantern should be able to submit a pull request through a reviewed tool call. The host-side tool uses a project-scoped GitHub identity to publish a proposal branch and open a PR against the canonical repository. Keep credentials outside the container and outside any process that runs Lantern's Git hooks, configuration, or tests. Run proposal tests without deployment credentials. Mike and the local project maintainer review proposed changes here before acceptance. Deploy from an explicitly selected, reviewed commit, independently of the writable checkout.

### Pre-deployment acceptance test

1. Compare the checkout with the canonical revision, branches, and tags. Verify that an older commit is readable, and that a write to the live release from the agent account is denied.
2. As Lantern, edit a tracked file, run a test, and commit on a proposal branch. Leave a second edit uncommitted and verify both edits survive container replacement.
3. Use the submission tool to open a test PR. Check that its base and diff match the proposal, its commits are attributed to Lantern, and the canonical branch and running revision still match their pre-test values.

A local Git smoke test on 2026-11-18 exercised the mirror, bundle, and clone steps with two commits, a second branch, and a tag. GitHub proposal submission is pending.

## Other agreed design decisions

- The terminal chat comes first. Messages and alarms should survive client disconnects. On idle heartbeats Lantern may pursue interests it chooses and adjust its cadence.
- Reserve OpenRouter spend before each request against a $10 UTC daily limit. Track OpenAI and Anthropic supervision costs separately so Mike can calibrate their allowance.
- Luna reviews proposed tool calls and periodically samples full context windows. Escalations go to Claude 3 Opus through Mike's legacy Anthropic API access. Supply the current date, deployment context, Mike's reason for entrusting Opus with review, concise evidence, Lantern's explanation, and read-only web research. Return detailed denials to Lantern and support voluntary consultation.
- Keep Lantern's versioned persona separate from the operating instructions. Reuse the Arcturus chess tools and test Qwen's tool calls, routing choices, and session resumption before moving on to independent wakeups and the later web UI.
