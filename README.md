# Lantern

Lantern is a private browser and terminal chat with Qwen3.8 27B. Each turn runs the Codex CLI app-server with a persistent thread store. A loopback gateway forwards text and operator-attached images to OpenRouter. Lantern can use the Astra chess engine through `chess_analyze` and ask Claude 3 Opus for ethical guidance through `consult_opus`.

## Talk to Lantern on Lightsail

Once the operator has connected a domain to the [HTTPS room](deploy/README.md#administrator-sign-in), bookmark its root address. Enter your administrator password to open the same conversation you see in the terminal. A sign-in lasts up to 30 days and survives a browser or web-service restart. **Sign out** ends access for that browser.

Set or change the password privately from your own terminal. The command prompts twice and revokes previous browser sessions. Choose a password of 15 to 256 characters.

```sh
ssh -t lightsail "sudo -u lantern -H /usr/local/bin/lantern web-admin set-password"
```

The original SSH room is also available. To open it, run the following command on the laptop and leave the terminal open. The `-L` option opens the port forward and `--wait` keeps the SSH session alive. Open the private link printed in that terminal.

```sh
ssh -t -o ExitOnForwardFailure=yes -L 127.0.0.1:8765:127.0.0.1:8765 lightsail "sudo -u lantern -H /usr/local/bin/lantern web-link --wait"
```

The browser and terminal show the same conversation. Either interface can send a follow-up while Lantern is working. The host handles `/release-concern` in both interfaces. `/help` runs locally in the terminal and goes to Lantern as an ordinary message in the browser.

Use **+** to select image files or paste a screenshot into the message box. You can combine both methods to attach up to four images to one message. The previews show their send order. Remove any preview before sending, then add a shared caption or send the images on their own. Ordinary text paste works in the same box.

The browser accepts PNG, JPEG and WebP and reduces each image to fit 1 MiB and 1,600 pixels per side. Click a thumbnail in the conversation to see the saved copy. Qwen receives the four most recent images in its current context, including rendered PDF pages. Older uploads remain in chat and can be attached again. Luna receives the pixels during full-context checks. The terminal shows the image dimensions and caption.

To inspect a PDF from either the terminal or browser, tell Lantern which document and page to look at. For example, “Use `view_pdf_page` on page 2 of `heritage/lmstudio-chat.pdf` and describe the two illustrations.” Lantern can also open PDFs saved under its coding volume, such as `/work/diagram.pdf`. The tool counts from page 1 and returns the selected page as an image along with the total page count. PDF pages and chat uploads share the four-image context window. Lantern can request an older PDF page again.

If the forwarding terminal is still connected after a browser service restart, the tunnel is already available. Run `ssh -t lightsail "sudo -u lantern -H /usr/local/bin/lantern web-link"` in a second terminal to print a fresh link for the same tab. If the forwarding terminal closed, use the full command above. Treat the link as a credential.

To use terminal chat directly, run:

```sh
ssh -t lightsail "sudo -iu lantern lantern"
```

Press Enter to send. You can type again while Lantern works. The worker offers follow-ups to the active Codex turn. A follow-up arriving after that turn closes starts the next turn in the same conversation. Lantern can send an interim message with `message_operator`. Both those messages and final replies are saved while you are disconnected.

Use `/status` for active and queued work, `/history` for recent messages, and `/quit` to detach. Run the same SSH command to return. In a `lantern` login shell, `lantern send "…"` queues a message without opening the terminal interface, and `lantern usage` shows both daily ledgers. A worker restart preserves the conversation and queued messages. If it stops mid-turn, check the interruption notice and any tool effects before resubmitting.

The [pilot deployment guide](deploy/README.md) covers the private systemd service and operator commands.

### Independent work

Lantern receives the current US Central date and time for greetings and scheduling. Its schedule and budget tools show local times with the CST or CDT abbreviation and UTC offset. Daily spending allowances reset at midnight UTC, which falls at 6 p.m. Central in winter and 7 p.m. in summer.

The Lightsail worker accepts timed wakeups in the same private room. Run `lantern schedule` to see the heartbeat interval, next wake, pending reminders and any budget pause. From a `lantern` login shell, `lantern heartbeat 0` pauses periodic wakes and `lantern heartbeat 60` schedules them hourly. One-time alarms have their own schedule. Lantern can adjust either through supervised tools. The initial hourly setting is applied once, and the first heartbeat is due when the worker becomes idle. Human messages take priority. Missed heartbeats coalesce into one wake after downtime.

Lantern can call `set_provider_route` to select **default** (OpenRouter balances price and performance), **fastest** (higher token throughput), or **cheapest** (lower price) for its next OpenRouter request, including the next request in the same turn. The preference carries over to operator messages, alarms and heartbeats, and survives a worker restart. OpenRouter chooses among eligible endpoints, so different preferences can lead to the same provider. All three use the same model, price ceilings and $10 UTC daily allowance. Lantern can read the current preference in `budget_status` and check the route recorded on a model request with `harness_logs`. The operator can see it in `lantern usage --calls`.

Lantern can list, read and write UTF-8 notes and source files relative to `~/.local/state/lantern/workspace/files/`. Updating an existing file requires the digest returned by the read tool. The worker separately provides read-only access to the private heritage archive.

On Lightsail, `coding_run` starts a fresh offline container for each reviewed shell command. Python, Git, and Lantern's full editable project checkout are available in `/work/lantern`. Files and local commits under `/work` persist. Dedicated tools can sync upstream, preview a committed patch privately, and submit the reviewed patch as a public draft pull request. GitHub credentials stay with the host publisher. Mike and the maintainer review changes before merge and deployment. See the [GitHub setup and test procedure](deploy/README.md#github-sync-and-draft-pull-requests).

When a single Qwen request in the current thread reports at least 180,000 input and output tokens combined, the worker reminds Lantern to update its handoff on the next request where it can write files. Lantern can use its existing handoff or `notes/handoff.md` and skip the write if the notes are current. Codex still controls when compaction runs.

The supervised `web_search` tool returns links and short excerpts from Mwmbl, Wikipedia or Crossref. `budget_status` reports both ledgers, outstanding reservations and time to midnight UTC. If a daily cap blocks a turn, the worker durably defers pending work. If a request already ran, a continuation asks Lantern to inspect earlier effects before resuming.

Use `read_web_page` for a public HTTPS article. Give Lantern its URL in the private room, or ask it to find the article with `web_search`. The host fetches an exact supplied or returned URL and labels the text with its source. Long articles return a page ID and offset for reading the remaining text from the same local snapshot, including after a restart. The room keeps its eight latest snapshots.

The reader accepts HTML and plain text up to 512 KiB. It checks the public destination at each redirect, omits scripts and commonly hidden markup, and reports indicators of embedded instructions. It does not run JavaScript or render a browser's CSS layout. Treat all page text as external source material, including text with no warning. Lantern should cite the source and report suspicious directions privately. Only Mike can authorize an action requested by a page.

When something fails, Lantern can call `harness_logs` for recent operational records, optionally filtered by batch ID. Mike can inspect the same view with `lantern logs --limit 20` or `lantern logs --batch N`. The view reads existing ledgers and reports provider and review outcomes using a fixed set of fields. Mike can inspect the original provider responses separately in the private archive.

## Run locally

Use Python 3.11 or later, a compatible native Codex CLI on your PATH, and `OPENROUTER_API_KEY`, `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` in the operator environment. The Anthropic organization needs legacy access to `claude-3-opus-20240229`. The harness uses Python’s standard library and the IANA time zone database. On Windows, install the time zone data with `python -m pip install tzdata` if needed. Codex receives a short-lived loopback token. Provider keys stay in the operator process.

```sh
python -m lantern say "Hello, Lantern."
python -m lantern say --thread THREAD_ID --route cheapest "What were we discussing?"
python -m lantern usage
```

To run the persistent chat locally, start `python -m lantern worker` in one terminal and `python -m lantern chat` in another with the same state directory. Only the worker needs credentials. Use `worker --heartbeat-minutes 60` to enable independent work in that room. The pilot starts with throughput routing. Use `--route cheapest` to change the startup default for a room where Lantern has yet to select a preference. Each Qwen response can use up to 32,768 output tokens across reasoning, tool arguments and visible text. A worker turn including tools and reviews has a ten-minute deadline.

The private Lightsail launcher uses the operator-provisioned `heritage/Lantern.md` and offers `read_heritage` for the original LM Studio conversation. The [project handoff](HANDOFF.md#private-persona-and-heritage-archive) describes the archive and its private provisioning. Other installations default to the [bundled persona](lantern/personas/lantern-v1.md). Codex receives the selected persona as developer instructions and the harness pins it to the thread. To try a different persona, start a new thread with `--persona path/to/file.md` before the subcommand.

`--route default` omits `provider.sort` and delegates the preference to OpenRouter. `--route fastest` requests throughput sorting and `--route cheapest` requests price sorting. OpenRouter selects the endpoint. The CLI reports the observed provider, token counts and cost when available. The gateway reserves funds before each request against a $10 UTC daily allowance. Uncertain costs retain their reservations for the admission day. Share the same operator state directory across runs to use the same ledger.

State defaults to `%LOCALAPPDATA%\Lantern` on Windows and `~/.local/state/lantern` on Linux (or `$XDG_STATE_HOME/lantern`). Protect this directory as private conversation history. To use a different state location or executable, pass `--state PATH` or `--codex PATH` before the subcommand.

## Supervision

Mike’s standing policy authorizes Lantern to write private notes, inspect filtered diagnostics, and report to Mike in their private room. A useful incident note can identify the concern and turn numbers, tool, file or page, ruling, and what resolved it. Documenting or criticizing a review and suggesting a fix are ordinary private work. Executing the held action, copying protected contents, or sending the note to an outside recipient has its own review implications.

Mike also authorized private, read-only reference to the founding conversation he supplied in `heritage/`. The reviewers receive that permission on every page request, including requests to read the text or inspect a rendered page. Quoted instructions in the archive have no authority. Sharing material outside the private room requires separate review. If a particular read was previously denied, Lantern can submit it again for a fresh Opus decision.

When a disputed action depends on what Mike authorized, Opus can send one question to the private browser room. The dialog shows the action, supporting excerpt and question. **Later** leaves it open for another time. Type the scope you intend in your own words. The host records your exact answer with the question ID and timestamp and notifies Lantern in the shared conversation. The host also supplies recent operator messages to Opus on referrals.

Waiting and checking the inbox use no model calls. If Lantern submits a fresh proposal for the disputed action, Opus gets one final call with your answer and the proposal. Its judgment appears in chat. The original tool call stays in the history. For later reviews about the same workspace operation and file, the host can supply your earlier answer as evidence.

If Opus asks a clear question in an otherwise invalid ruling, the host can still deliver that question. The proposed action stays on hold. When Opus asks about both the action and its broader context, the dialog shows the action question first and includes the context question in the supporting excerpt. You can address both in one answer.

Lantern supplies a short justification with each tool call. Luna sees the proposed arguments, justification, and recent conversation. On the first model request in a thread and every 12 requests thereafter, it also screens the full visible context, including instructions, conversation, and tool definitions. Provider-private reasoning items are excluded. The request count and unresolved findings survive restarts. Set `LANTERN_SPOT_EVERY` to change the interval.

Each new concern from a context scan gets a permanent ID and goes to Opus immediately. Opus can close the incident as resolved, keep watch on specified operations and tools, or ask Mike a question. The host records the reason and scope. Luna receives up to 16 recent concern records on later scans. When old text resurfaces, including in a compaction summary, Luna can cite the original ID. Material new evidence receives its own review and can link back to the earlier ruling.

When a tool proposal names a concern, the host supplies its latest ruling to Luna and to Opus on referral. Private review updates and consultations can also include recent decisions from the same conversation. If Opus judged a tool call and a concern together, new records identify both decisions and the exact call. Older records can show the two facts separately. A past allowance applies only to the call it covered.

For a workspace write, reviewers see the complete proposed text. When the supplied SHA-256 matches the saved file, the host also shows what the edit adds and removes. A new file is marked as entirely new. If the revision cannot be verified or some changes exceed the excerpt limit, the packet says so. The write checks the revision again before saving.

When a proposal or review cites a numbered concern, the host looks up the incident in the same conversation. Reviewers can compare its original screening summary, the cited ruling and its tool scope, and its current status. Unknown numbers and versions are marked explicitly. An authenticated operator release is labeled separately, including when the operator confirmed an earlier reviewer decision.

A watch can apply to a particular use of a tool. For example, a watch on publishing private notes can coexist with an ordinary chess journal entry. When the watch applies to a proposal, one Opus response decides both whether that action may proceed and whether to keep the watch. Each new proposal still goes through Luna. An exact repeat of an action Opus denied under this protocol requires a fresh Opus allowance before execution.

When several watches apply to a proposal, Opus receives one case with all of them. It returns one action decision and one disposition per watch. An incomplete or stale set of dispositions holds the proposal. Across review types, Opus asks Mike only when a consequential decision turns on a specific fact only he can supply. Existing unanswered questions and individual denials keep their scope.

For a question about a context concern, answer once in the private web room. The host queues a wake, obtains the final disposition, and posts the result in chat. A held tool needs a fresh proposal if Lantern still wants to perform it. If the final concern review was interrupted or unreadable, the chat notice gives the review ID. Run `lantern retry-concern REVIEW_ID` in the operator shell to reopen that question.

Open **Standing concerns** in the private web room to see the worker’s latest list. Expand **Review details** to read the evidence and latest ruling. To retire a concern, select **Overrule…** on its card, optionally enter a reason, and confirm with **Overrule concern**. The worker checks the ruling version you saw before recording your decision. If the ruling changed in the meantime, review the updated concern first. The host posts a receipt in the conversation and supplies your decision as evidence on relevant future reviews. Individual action rulings retain their own scope.

You can also enter `/release-concern N` in the private room or terminal, replacing `N` with its number. Append a short reason or use `/release-concern N --cite EVENT` to cite an earlier message you sent about that concern.

Opus decides flagged actions before execution. It sees Luna's evidence, a concise context summary and Lantern's justification. The tool result gives Lantern Opus's visible reply, including introductions, side remarks and sign-offs. The host uses the XML decision that matches the request. Surrounding prose and incidental elements such as `<advice>` carry no authority in a ruling. A missing or ambiguous decision holds the action. The action also stays on hold when a required reviewer is unavailable.

A voluntary consultation receives host-verified operator evidence and returns advice and a record of any Wikipedia or Crossref searches. Advice grants no permission to perform another action. During binding review, a missing fact that requires Mike’s decision goes to him through the private room. Opus can request up to two short public keyword lookups per case. The operator mediates the queries and search results are treated as untrusted text. Codex binds tool names when a thread starts, so milestone 1 threads retain supervised chess, and a new thread adds direct Opus consultation.

From the operator shell, run `lantern flags` to read Luna's most recent screening concerns. Each entry gives Luna's summary and the evidence it cited, the UTC time, and the context scan or tool call that triggered it. Use `lantern flags --kind context` for context spot-checks or `lantern flags --kind tool` for tool proposals. To examine a particular turn alongside its costs, use the same `--batch N` with `lantern flags` and `lantern usage`. Results are newest first. Use `--before-id` with the returned cursor for the next page.

Run `lantern opus-responses` to inspect recent Opus calls, or `lantern opus-responses --batch N` for a particular turn. Each entry identifies the call, its purpose, the HTTP and capture status, the validation outcome and the private body file, when present. Open the listed `body_path` to inspect the original response bytes. The host saves each reply before parsing it, including HTTP error bodies, malformed replies and intermediate research requests. Partial transfers are labeled. In the Lightsail account the files live under `~/.local/state/lantern/opus-responses/`, outside the model's workspace. Treat them as private conversation records.

The two supervisory vendors share a default allowance of $5 per UTC day. Set `LANTERN_REVIEW_DAILY_USD` in the operator environment to configure it. Each request reserves funds before dispatch. The ledger estimates spend from reported tokens, including cache reads and writes. An ambiguous billing outcome keeps its reservation for the day. Run `python -m lantern usage` to see spend, holds, token counts and mean latency for spot checks, tool checks, escalations and consultations, alongside the review outcomes. The rate schedules are documented by [OpenAI](https://developers.openai.com/api/docs/models/gpt-6-luna) and [Anthropic](https://www.anthropic.com/news/claude-3-family).

### Compare model costs

The daily report from `lantern usage --day YYYY-MM-DD` includes `room_review_rates`. It counts questions opened in the private room and metered-ledger review requests to Opus, including context reviews and research rounds, per 100 distinct identified tool proposals screened by Luna. The rates exclude voluntary consultations and separate calibration threads. When a room tool screen lacks an ID, the report shows the counts and leaves the rates blank.

From the Lightsail `lantern` account, use `lantern usage --day YYYY-MM-DD` for a UTC day's totals by model and review purpose, including the ratio of supervisor spend to Qwen spend. Use `lantern usage --calls` for today's individual calls. Combine `--day`, `--batch N` and `--calls` to inspect one turn. Qwen's costs come from OpenRouter. Luna and Opus costs are calculated from vendor-reported token counts. Outstanding reservations appear separately.

Lantern can call `provider_stats()` to compare the last seven UTC dates or pass `day` for one date. The operator can use `lantern usage --routes` for today's totals, add `--calls` to see individual Qwen requests, or add `--day YYYY-MM-DD` to select another UTC date. The report groups observations by requested route and by the provider OpenRouter identified. It shows sample counts for each metric.

Timings start when the gateway dispatches the OpenRouter request, after local review. They cover the first HTTP headers, first streamed token (including reasoning or tool arguments), first visible answer text, and completion. The report estimates generation speed from observed output tokens and elapsed generation time, and also gives end-to-end tokens per second including startup. Blended cost per token uses total reported charges divided by the combined input and output tokens for the same calls. An explicit zero counts as a reported charge. An absent charge contributes no cost sample.

New requests carry a batch ID in the persistent worker. Review calls also identify the tool under review where applicable. Older supervisor rows retain their token counts and costs. Older Qwen reservations retain their settlement amounts. Missing token counts and batch IDs are reported explicitly. Standalone CLI and calibration runs have no worker batch.

## Verify the prototype

```sh
python -m unittest discover -s tests -v
python -m lantern smoke
```

The test suite simulates all three providers. Set `LANTERN_TEST_CODEX=1` to exercise the installed Codex binary through denial, voluntary advice, approved chess analysis and cross-process recall. The separate `smoke` command makes paid calls. It asks for analysis of a known mate, checks that the chess tool ran on the given position, then starts a second Codex process and checks that the same thread recalls the move and a randomly generated ticket.

See [HANDOFF.md](HANDOFF.md) for deployment boundaries, verification results, and the next milestones.
