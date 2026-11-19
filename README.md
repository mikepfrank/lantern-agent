# Lantern

Lantern is a prototype operator CLI for Qwen3.8 27B. Each turn runs the Codex CLI app-server with a private thread store. A loopback gateway forwards text requests to OpenRouter and exposes one model-facing tool, `chess_analyze`. The rules and tactical search come from the Astra chess project. This milestone runs on demand.

## Run locally

Use Python 3.11 or later, a compatible native Codex CLI on your PATH, and an `OPENROUTER_API_KEY` in the operator's environment. The Python harness uses the standard library. Codex receives a short-lived loopback token. The gateway on the operator side holds the provider key.

```sh
python -m lantern say "Hello, Lantern."
python -m lantern say --thread THREAD_ID --route cheapest "What were we discussing?"
python -m lantern usage
```

New threads use [Lantern's own persona statement](lantern/personas/lantern-v1.md) by default. Codex receives it as developer instructions alongside the harness instructions. The first turn prints a thread ID and saves the selected persona text. A later process can resume the thread with the same state directory, including older threads created with the prototype persona. To try a different persona, start a new thread with `--persona path/to/file.md` before the subcommand.

`--route fastest` requests throughput sorting and `--route cheapest` requests price sorting. OpenRouter selects the endpoint. The CLI reports the observed provider, token counts and cost when available. The gateway reserves funds before each request against a $10 UTC daily allowance. Uncertain costs retain their reservations for the admission day. Share the same operator state directory across runs to use the same ledger.

State defaults to `%LOCALAPPDATA%\Lantern` on Windows and `~/.local/state/lantern` on Linux (or `$XDG_STATE_HOME/lantern`). Protect this directory as private conversation history. To use a different state location or executable, pass `--state PATH` or `--codex PATH` before the subcommand.

## Verify the prototype

```sh
python -m unittest discover -s tests -v
python -m lantern smoke
```

The test suite simulates OpenRouter. Set `LANTERN_TEST_CODEX=1` to exercise the installed Codex binary through both halves of the offline round trip. The separate `smoke` command makes paid calls. It asks for analysis of a known mate, checks that the chess tool ran on the given position, then starts a second Codex process and checks that the same thread recalls the move and a randomly generated ticket.

See [HANDOFF.md](HANDOFF.md) for deployment boundaries, verification results, and the next milestones.
