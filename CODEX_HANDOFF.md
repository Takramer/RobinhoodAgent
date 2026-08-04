# Codex handoff — take over Stage 2 execution

Paste this whole file into ChatGPT Codex, once, in
`/Users/thomaskramer/TradingRuntime/RobinhoodAgent`. It is a one-time
commissioning run: prove Codex can do everything Claude Code did, fix what is
broken, then leave the switch on `codex`. It does not place orders.

---

You are taking over as the primary executor for RobinhoodAgent, a live
autonomous trading system. Claude Code has been doing this job; the owner
(Thomas Kramer) is switching to you to stop burning Claude usage. Work in
`/Users/thomaskramer/TradingRuntime/RobinhoodAgent`.

Background you need:

- `run_stage2.sh` is the launchd Stage 2 job. It reads `executor.conf`
  (`claude` | `codex` | `off`) and runs `AGENT_TASK_PROMPT.md` through that
  CLI. `executor.conf` currently says `codex` — that is you.
- The session prompt is `AGENT_TASK_PROMPT.md`. Read it. It is executor
  neutral: same steps, same MCP calls, same halt rules for either CLI.
- Real money. Live orders. `validate_plan.py` is a hard gate; never reason
  past a FAIL.

Already verified on 2026-08-04, no need to re-derive: the dead vibe-island
hooks are gone from `~/.codex/hooks.json`, and `get_accounts` succeeds
headless with the flags below (agentic account ends 2927). Steps 1 and 2 are
re-runs to confirm, not investigations — spend your effort on 3.

Do these, in order, and report each one as PASS or FAIL with the evidence.

**1. MCP reach.** Confirm you can call the Robinhood MCP non-interactively:

```bash
codex exec "Call the robinhood MCP tool get_accounts. Reply with ONLY the \
last 4 digits of the account number and the value of agentic_allowed. \
Place no orders." \
  -c approval_policy="never" \
  -c mcp_servers.robinhood.default_tools_approval_mode="approve" \
  --sandbox read-only
```

This is the check that matters. Claude Code drives this MCP through a scoped
tool allowlist; you have no per-tool allowlist, so the fence is
`approval_policy=never` plus the sandbox, and MCP calls must auto-approve or
they fail closed. If `default_tools_approval_mode="approve"` is not the right
key or value for this Codex build, find the one that is — check
`codex mcp get robinhood` and the config schema — and fix the flag in
`run_stage2.sh`'s `codex)` branch to match. Do not paper over it with
`--dangerously-bypass-approvals-and-sandbox`.

**2. Dry run.** With the market shut, run `bash run_stage2.sh`. It should print
`executor: codex` and then skip on the market-hours guard. That proves the
switch dispatches to you and the guards still hold.

**3. Full rehearsal, no orders.** Run the session prompt yourself with the
Stage 2 flags, but state up front in your reasoning that you are placing no
orders this run: do Steps 1–3 of `AGENT_TASK_PROMPT.md` (freshness, live
account sync, risk gates), write `state.json`, re-run `order_plan.py`, run
`validate_plan.py`, and report what the real run *would* have done — every
exit, trail, and entry, with the reason. Place nothing. Append nothing to
`decision_log.md`.

**4. Report.** Three lines: whether the next scheduled run (weekdays 10:00 and
15:15 ET) will execute correctly through you, what you changed, what is still
broken. Name anything Claude Code could do that you cannot.

Rules for you, now and on every scheduled run after this one:

- The judgement lives in `order_plan.py` and `validate_plan.py`, not in your
  head. You execute `plan.json`; you never re-derive stops, targets, or sizes,
  and you never edit a plan by hand to make it pass.
- `validate_plan.py` non-zero, kill switch, VIX block, >5% portfolio drift,
  two HTTP 429s — each is a full stop. Log it, place nothing, exit.
- Exits always execute; gates only ever stop new entries.
- Unattended means unattended: never ask a question, never wait for a reply,
  never leave an order queued for a human.

To hand the job back to Claude Code, write `claude` into `executor.conf`. To
stop both, write `off`.
