# Lantern project handoff

## Current state

As of 2026-11-18, Lantern is a new local repository. The first milestone is a Codex CLI session driven by Qwen3.8 27B through OpenRouter that can use a chess tool and resume its conversation after disconnecting. Mike will relay Lantern's persona. The GitHub remote, container, and Lightsail service have yet to be provisioned.

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
