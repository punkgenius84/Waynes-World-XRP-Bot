a discord hook for XRP 3-4 hr reports and Alerts

## How it runs (GitHub Actions)

| Workflow | When | `RUN_MODE` | Does |
|---|---|---|---|
| `xrp-empire-scheduled.yml` | 6x/day + manual | `report` | Full Discord reports, X posts (XRP/BTC), news, saves `history/*.csv`, `last_alert.json`, `surge_state.json` |
| `xrp-empire-surge.yml` | every ~12 min | `surge` | 60-minute move check only. Alerts to Discord; saves `surge_state.json` (cooldown) |

`RUN_MODE=surge` matters: both workflows fire as GitHub `schedule` events, so the event name alone
can't tell a surge poll from a report run.

## Secrets

`DISCORD_WEBHOOK_<COIN>` (XRP BTC ETH ADA SOL HBAR ZEC), `DISCORD_WEBHOOK_NEWS`,
`X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET`.
Optional but recommended: `CRYPTOCOMPARE_API_KEY` (keyless calls from shared runner IPs get throttled).
Optional: `GH_PAT` (the workflows fall back to the built-in token).
