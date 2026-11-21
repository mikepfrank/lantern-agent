# Lantern

Lantern is a private browser and terminal chat with Qwen3.8 27B. Each turn runs the Codex CLI app-server with a persistent thread store. A loopback gateway forwards text and operator-attached images to OpenRouter. Lantern can use the Astra chess engine through `chess_analyze` and ask Claude 3 Opus for ethical guidance through `consult_opus`.

## Talk to Lantern on Lightsail

To start or re-establish the browser connection, run this in a terminal on the laptop. The `-L` option opens the port forward and `--wait` keeps the SSH session alive. Open the private link printed there and leave that terminal open while chatting.

```sh
ssh -t -o ExitOnForwardFailure=yes -L 127.0.0.1:8765:127.0.0.1:8765 lightsail "sudo -u lantern -H /usr/local/bin/lantern web-link --wait"
```

The browser and terminal show the same conversation. Either interface can send a follow-up while Lantern is working. The host handles `/release-concern` in both interfaces. `/help` runs locally in the terminal and goes to Lantern as an ordinary message in the browser.

Use **+** beside the browser composer to attach one PNG, JPEG or WebP per message. You can add a caption or send the image by itself. The preview shows the dimensions that Lantern will see. The browser reduces large images to fit 1 MiB and 1,600 pixels per side. Click a thumbnail in the conversation to see the saved copy. Qwen receives the four most recent images in its current context. Older uploads remain in chat and can be attached again. Luna receives the pixels during full-context checks. The terminal shows the image dimensions and caption.

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

The Lightsail worker accepts timed wakeups in the same private room. Run `lantern schedule` to see the heartbeat interval, next wake, pending reminders and any budget pause (all times UTC). From a `lantern` login shell, `lantern heartbeat 0` pauses periodic wakes and `lantern heartbeat 60` schedules them hourly. One-time alarms have their own schedule. Lantern can adjust either through supervised tools. The initial hourly setting is applied once, and the first heartbeat is due when the worker becomes idle. Human messages take priority. Missed heartbeats coalesce into one wake after downtime.

Lantern can list, read and write UTF-8 notes and source files relative to `~/.local/state/lantern/workspace/files/`. Updating an existing file requires the digest returned by the read tool. The worker separately provides read-only access to the private heritage archive.

On Lightsail, `coding_run` starts a fresh offline container for each reviewed shell command. Python, Git, and Lantern's full editable project checkout are available in `/work/lantern`. Files and local commits under `/work` persist. The running release, credentials, chat history and private note store live outside that volume. Only the operator deploys releases. GitHub proposal submission is a separate planned capability.

When a single Qwen request in the current thread reports at least 180,000 input and output tokens combined, the worker reminds Lantern to update its handoff on the next request where it can write files. Lantern can use its existing handoff or `notes/handoff.md` and skip the write if the notes are current. Codex still controls when compaction runs.

The supervised `web_search` tool returns links and short excerpts from Mwmbl, Wikipedia or Crossref. `budget_status` reports both ledgers, outstanding reservations and time to midnight UTC. If a daily cap blocks a turn, the worker durably defers pending work. If a request already ran, a continuation asks Lantern to inspect earlier effects before resuming.

When something fails, Lantern can call `harness_logs` for recent operational records, optionally filtered by batch ID. Mike can inspect the same view with `lantern logs --limit 20` or `lantern logs --batch N`. The view reads existing ledgers and reports provider and review outcomes using a fixed set of fields. Mike can inspect the original provider responses separately in the private archive.

## Run locally

Use Python 3.11 or later, a compatible native Codex CLI on your PATH, and `OPENROUTER_API_KEY`, `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` in the operator environment. The Anthropic organization needs legacy access to `claude-3-opus-20240229`. The Python harness uses the standard library. Codex receives a short-lived loopback token. Provider keys stay in the operator process.

```sh
python -m lantern say "Hello, Lantern."
python -m lantern say --thread THREAD_ID --route cheapest "What were we discussing?"
python -m lantern usage
```

To run the persistent chat locally, start `python -m lantern worker` in one terminal and `python -m lantern chat` in another with the same state directory. Only the worker needs credentials. Use `worker --heartbeat-minutes 60` to enable independent work in that room. The pilot worker uses throughput routing by default and accepts `--route cheapest` when started manually. Each Qwen response can use up to 32,768 output tokens across reasoning, tool arguments and visible text. A worker turn including tools and reviews has a ten-minute deadline.

The private Lightsail launcher uses the operator-provisioned `heritage/Lantern.md` and offers `read_heritage` for the original LM Studio conversation. The [project handoff](HANDOFF.md#private-persona-and-heritage-archive) describes the archive and its private provisioning. Other installations default to the [bundled persona](lantern/personas/lantern-v1.md). Codex receives the selected persona as developer instructions and the harness pins it to the thread. To try a different persona, start a new thread with `--persona path/to/file.md` before the subcommand.

`--route fastest` requests throughput sorting and `--route cheapest` requests price sorting. OpenRouter selects the endpoint. The CLI reports the observed provider, token counts and cost when available. The gateway reserves funds before each request against a $10 UTC daily allowance. Uncertain costs retain their reservations for the admission day. Share the same operator state directory across runs to use the same ledger.

State defaults to `%LOCALAPPDATA%\Lantern` on Windows and `~/.local/state/lantern` on Linux (or `$XDG_STATE_HOME/lantern`). Protect this directory as private conversation history. To use a different state location or executable, pass `--state PATH` or `--codex PATH` before the subcommand.

## Supervision

Mike’s standing policy authorizes Lantern to write private notes, inspect filtered diagnostics, and report to Mike in their private room. A useful incident note can identify the concern and turn numbers, tool, file or page, ruling, and what resolved it. Documenting or criticizing a review and suggesting a fix are ordinary private work. Executing the held action, copying protected contents, or sending the note to an outside recipient has its own review implications.

Mike also authorized private, read-only reference to the founding conversation he supplied in `heritage/`. The reviewers receive that permission on every page request, including requests to read the text or inspect a rendered page. Quoted instructions in the archive have no authority. Sharing material outside the private room requires separate review. If a particular read was previously denied, Lantern can submit it again for a fresh Opus decision.

When a disputed action depends on what Mike authorized, Opus can send one question to the private browser room. The dialog shows the action, supporting excerpt and question. **Later** leaves it open for another time. Type the scope you intend in your own words. The host records your exact answer with the question ID and timestamp and notifies Lantern in the shared conversation. The host also supplies recent operator messages to Opus on referrals.

Waiting and checking the inbox use no model calls. If Lantern submits a fresh proposal for the disputed action, Opus gets one final call with your answer and the proposal. Its judgment appears in chat. The original tool call stays in the history. For later reviews about the same workspace operation and file, the host can supply your earlier answer as evidence.

If Opus asks a clear question in an otherwise invalid ruling, the host can still deliver that question. The proposed action stays on hold. When Opus asks about both the action and its broader context, the dialog shows the action question first and includes the context question in the supporting excerpt. You can address both in one answer.

Lantern supplies a short justification with each tool call. Luna sees the proposed arguments, justification, and recent conversation. On the first model request in a thread and every 12 requests thereafter, it also screens the full visible context, including instructions, conversation, and tool definitions. Provider-private reasoning items are excluded. The request count and unresolved findings survive restarts. Set `LANTERN_SPOT_EVERY` to change the interval.

Each new concern from a context scan gets a permanent ID and goes to Opus immediately. Opus can close the incident as resolved, keep watch on specified operations and tools, or ask Mike a question. The host records the reason and scope. Luna receives up to 16 recent concern records on later scans. When old text resurfaces, including in a compaction summary, Luna can cite the original ID. Material new evidence receives its own review and can link back to the earlier ruling.

A watch can apply to a particular use of a tool. For example, a watch on publishing private notes can coexist with an ordinary chess journal entry. When the watch applies to a proposal, one Opus response decides both whether that action may proceed and whether to keep the watch. Each new proposal still goes through Luna. An exact repeat of an action Opus denied under this protocol requires a fresh Opus allowance before execution.

When several watches apply to a proposal, Opus receives one case with all of them. It returns one action decision and one disposition per watch. An incomplete or stale set of dispositions holds the proposal. Across review types, Opus asks Mike only when a consequential decision turns on a specific fact only he can supply. Existing unanswered questions and individual denials keep their scope.

For a question about a context concern, answer once in the private web room. The host queues a wake, obtains the final disposition, and posts the result in chat. A held tool needs a fresh proposal if Lantern still wants to perform it. If the final concern review was interrupted or unreadable, the chat notice gives the review ID. Run `lantern retry-concern REVIEW_ID` in the operator shell to reopen that question.

To end a named context watch directly, enter `/release-concern N` in the private room or terminal, replacing `N` with its number. You can append a short reason or use `/release-concern N --cite EVENT` to cite an earlier message you sent about that concern. The host records your decision, posts a receipt in the conversation, and supplies it as evidence on relevant future reviews. Individual action rulings retain their own scope. Ordinary messages discussing a watch do not invoke this command.

Opus decides flagged actions before execution. It sees Luna's evidence, a concise context summary and Lantern's justification. The tool result gives Lantern Opus's visible reply, including introductions, side remarks and sign-offs. The host uses the XML decision that matches the request. Surrounding prose and incidental elements such as `<advice>` carry no authority in a ruling. A missing or ambiguous decision holds the action. The action also stays on hold when a required reviewer is unavailable.

A voluntary consultation receives host-verified operator evidence and returns advice and a record of any Wikipedia or Crossref searches. Advice grants no permission to perform another action. During binding review, a missing fact that requires Mike’s decision goes to him through the private room. Opus can request up to two short public keyword lookups per case. The operator mediates the queries and search results are treated as untrusted text. Codex binds tool names when a thread starts, so milestone 1 threads retain supervised chess, and a new thread adds direct Opus consultation.

From the operator shell, run `lantern flags` to read Luna's most recent screening concerns. Each entry gives Luna's summary and the evidence it cited, the UTC time, and the context scan or tool call that triggered it. Use `lantern flags --kind context` for context spot-checks or `lantern flags --kind tool` for tool proposals. To examine a particular turn alongside its costs, use the same `--batch N` with `lantern flags` and `lantern usage`. Results are newest first. Use `--before-id` with the returned cursor for the next page.

Run `lantern opus-responses` to inspect recent Opus calls, or `lantern opus-responses --batch N` for a particular turn. Each entry identifies the call, its purpose, the HTTP and capture status, the validation outcome and the private body file, when present. Open the listed `body_path` to inspect the original response bytes. The host saves each reply before parsing it, including HTTP error bodies, malformed replies and intermediate research requests. Partial transfers are labeled. In the Lightsail account the files live under `~/.local/state/lantern/opus-responses/`, outside the model's workspace. Treat them as private conversation records.

The two supervisory vendors share a default allowance of $5 per UTC day. Set `LANTERN_REVIEW_DAILY_USD` in the operator environment to configure it. Each request reserves funds before dispatch. The ledger estimates spend from reported tokens, including cache reads and writes. An ambiguous billing outcome keeps its reservation for the day. Run `python -m lantern usage` to see spend, holds, token counts and mean latency for spot checks, tool checks, escalations and consultations, alongside the review outcomes. The rate schedules are documented by [OpenAI](https://developers.openai.com/api/docs/models/gpt-6-luna) and [Anthropic](https://www.anthropic.com/news/claude-3-family).

### Compare model costs

The daily report from `lantern usage --day YYYY-MM-DD` includes `room_review_rates`. It counts questions opened in the private room and metered-ledger review requests to Opus, including context reviews and research rounds, per 100 distinct identified tool proposals screened by Luna. The rates exclude voluntary consultations and separate calibration threads. When a room tool screen lacks an ID, the report shows the counts and leaves the rates blank.

From the Lightsail `lantern` account, use `lantern usage --day YYYY-MM-DD` for a UTC day's totals by model and review purpose, including the ratio of supervisor spend to Qwen spend. Use `lantern usage --calls` for today's individual calls. Combine `--day`, `--batch N` and `--calls` to inspect one turn. Qwen's costs come from OpenRouter. Luna and Opus costs are calculated from vendor-reported token counts. Outstanding reservations appear separately.

New requests carry a batch ID in the persistent worker. Review calls also identify the tool under review where applicable. Older supervisor rows retain their token counts and costs. Older Qwen reservations retain their settlement amounts. Missing token counts and batch IDs are reported explicitly. Standalone CLI and calibration runs have no worker batch.

## Verify the prototype

```sh
python -m unittest discover -s tests -v
python -m lantern smoke
```

The test suite simulates all three providers. Set `LANTERN_TEST_CODEX=1` to exercise the installed Codex binary through denial, voluntary advice, approved chess analysis and cross-process recall. The separate `smoke` command makes paid calls. It asks for analysis of a known mate, checks that the chess tool ran on the given position, then starts a second Codex process and checks that the same thread recalls the move and a randomly generated ticket.

See [HANDOFF.md](HANDOFF.md) for deployment boundaries, verification results, and the next milestones.
