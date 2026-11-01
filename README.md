# XRP Market Report Bot

Automated XRP market analysis and Discord reporting, including multi-timeframe structure, key levels, invalidation status, volume context, and chart output. News collection and X posting remain available as optional integrations and are disabled by default.

## How it runs (GitHub Actions)

| Workflow | When | `RUN_MODE` | Does |
|---|---|---|---|
| `xrp-empire-scheduled.yml` | 6x/day + manual | `report` | XRP market report to Discord; optional X/news integrations; saves `history/*.csv`, `last_alert.json`, and `surge_state.json` |

The separate 12-minute surge workflow is **currently disabled**. Its file is retained as `.github/workflows/xrp-empire-surge.yml.disabled`, so GitHub Actions will not schedule it. When enabled, it would run five times per hour (`:06, :18, :30, :42, :54`) and check for 60-minute price moves only. It is not part of the bot's current automatic schedule.

The active scheduled workflow runs the normal report six times per day. Its `RUN_MODE=report` setting keeps that behavior distinct from the optional `RUN_MODE=surge` path.

## Optional integrations and secrets

`DISCORD_WEBHOOK_XRP` is used for the market report. Optional integrations: set `enable_news: true` and configure `DISCORD_WEBHOOK_NEWS` to enable news collection/posting; set `post_reports_to_x: true` and configure `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, and `X_ACCESS_SECRET` to enable X posting. Both integrations are disabled by default.

To enable the optional integrations later, users can change `enable_news` or `post_reports_to_x` in `config.json` and configure their respective credentials. Leave both settings `false` to keep the bot focused on XRP market reports only.

Optional but recommended: `CRYPTOCOMPARE_API_KEY` (keyless calls from shared runner IPs get throttled).
Optional: `GH_PAT` (the workflows fall back to the built-in token).
