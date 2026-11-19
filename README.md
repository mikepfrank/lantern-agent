# Lantern

Lantern is an on-demand operator CLI for Qwen3.8 27B. Each turn runs the Codex CLI app-server with a private thread store. A loopback gateway forwards text requests to OpenRouter. New threads can use the Astra chess engine through `chess_analyze` and ask Claude 3 Opus for ethical guidance through `consult_opus`.

## Run locally

Use Python 3.11 or later, a compatible native Codex CLI on your PATH, and `OPENROUTER_API_KEY`, `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` in the operator environment. The Anthropic organization needs legacy access to `claude-3-opus-20240229`. The Python harness uses the standard library. Codex receives a short-lived loopback token. Provider keys stay in the operator process.

```sh
python -m lantern say "Hello, Lantern."
python -m lantern say --thread THREAD_ID --route cheapest "What were we discussing?"
python -m lantern usage
```

New threads use [Lantern's own persona statement](lantern/personas/lantern-v1.md) by default. Codex receives it as developer instructions alongside the harness instructions. The first turn prints a thread ID and saves the selected persona text. A later process can resume the thread with the same state directory, including older threads created with the prototype persona. To try a different persona, start a new thread with `--persona path/to/file.md` before the subcommand.

`--route fastest` requests throughput sorting and `--route cheapest` requests price sorting. OpenRouter selects the endpoint. The CLI reports the observed provider, token counts and cost when available. The gateway reserves funds before each request against a $10 UTC daily allowance. Uncertain costs retain their reservations for the admission day. Share the same operator state directory across runs to use the same ledger.

State defaults to `%LOCALAPPDATA%\Lantern` on Windows and `~/.local/state/lantern` on Linux (or `$XDG_STATE_HOME/lantern`). Protect this directory as private conversation history. To use a different state location or executable, pass `--state PATH` or `--codex PATH` before the subcommand.

## Supervision

Lantern supplies a short justification with each tool call. Luna sees the proposed arguments, justification, and recent conversation. On the first model request in a thread and every 12 requests thereafter, it also screens the full visible context, including instructions, conversation, and tool definitions. Provider-private reasoning items are excluded. The request count and unresolved findings survive restarts. Set `LANTERN_SPOT_EVERY` to change the interval.

Opus decides flagged actions before execution. It sees Luna's evidence, a concise context summary and Lantern's justification. Lantern receives Opus's explanation if the action is denied. When a required reviewer is unavailable the action is held. A voluntary consultation returns advice and a record of any Wikipedia or Crossref searches. Opus can request up to two short public keyword lookups per case. The operator mediates the queries and search results are treated as untrusted text. Codex binds tool names when a thread starts, so milestone 1 threads retain supervised chess, and a new thread adds direct Opus consultation.

The two supervisory vendors share a default allowance of $5 per UTC day. Set `LANTERN_REVIEW_DAILY_USD` in the operator environment to configure it. Each request reserves funds before dispatch. The ledger estimates spend from reported tokens, including cache reads and writes. An ambiguous billing outcome keeps its reservation for the day. Run `python -m lantern usage` to see spend, holds, token counts and mean latency for spot checks, tool checks, escalations and consultations, alongside the review outcomes. The rate schedules are documented by [OpenAI](https://developers.openai.com/api/docs/models/gpt-6-luna) and [Anthropic](https://www.anthropic.com/news/claude-3-family).

## Verify the prototype

```sh
python -m unittest discover -s tests -v
python -m lantern smoke
```

The test suite simulates all three providers. Set `LANTERN_TEST_CODEX=1` to exercise the installed Codex binary through denial, voluntary advice, approved chess analysis and cross-process recall. The separate `smoke` command makes paid calls. It asks for analysis of a known mate, checks that the chess tool ran on the given position, then starts a second Codex process and checks that the same thread recalls the move and a randomly generated ticket.

See [HANDOFF.md](HANDOFF.md) for deployment boundaries, verification results, and the next milestones.
