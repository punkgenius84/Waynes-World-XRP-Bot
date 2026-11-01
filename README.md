# XRP Market Report Bot

Automated XRP market analysis and Discord reporting, including multi-timeframe structure, key levels, invalidation status, volume context, and chart output. News collection and X posting remain available as optional integrations and are disabled by default.


## How it runs (GitHub Actions)

| Workflow | When | `RUN_MODE` | Does |
|---|---|---|---|
| `xrp-empire-scheduled.yml` | 6x/day + manual | `report` | XRP market report to Discord; optional X/news integrations; saves `history/*.csv`, `last_alert.json`, `surge_state.json` |
| `xrp-empire-surge.yml` | every ~12 min | `surge` | 60-minute move check only. Alerts to Discord; saves `surge_state.json` (cooldown) |

`RUN_MODE=surge` matters: both workflows fire as GitHub `schedule` events, so the event name alone
can't tell a surge poll from a report run.

## Optional integrations and secrets

`DISCORD_WEBHOOK_XRP` is used for the market report. Optional integrations: set `enable_news: true` and configure `DISCORD_WEBHOOK_NEWS` to enable news collection/posting; set `post_reports_to_x: true` and configure `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, and `X_ACCESS_SECRET` to enable X posting. Both integrations are disabled by default.
Optional but recommended: `CRYPTOCOMPARE_API_KEY` (keyless calls from shared runner IPs get throttled).
Optional: `GH_PAT` (the workflows fall back to the built-in token).
