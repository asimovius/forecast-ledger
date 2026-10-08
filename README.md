# ForecastLedger — MCP Server

Public MCP (Model Context Protocol) server for the
[ForecastLedger](https://github.com/asimovius) graded BTC market-forecast
registry: four horizons (24h / 3d / 7d / 4w), every call timestamped,
graded against outcomes, and published with its calibration stats — whether
they flatter the model or not.

**This dataset provides critical decision-making data points. It is not
advice, not a signal service, and not personalised.**

[![CI](https://github.com/asimovius/forecast-ledger/actions/workflows/ci.yml/badge.svg)](https://github.com/asimovius/forecast-ledger/actions/workflows/ci.yml)
![license](https://img.shields.io/badge/license-MIT-blue)

## What it serves

A read-only view of `registry.json` — derived artifacts only:

- `get_registry_summary` — headline stats per horizon: `n_graded`, `hit_rate`,
  `brier`, `baseline_brier` (naive baseline: 0.25), and `armed`
  (a horizon's stats are marked un-armed below its minimum sample count).
- `get_horizon_calls` — the calls for one horizon, optional 
  `status` filter (`resolved` or `open`), each with `id`, issue time,
  direction (`Bullish`/`Bearish`/`Neutral`), probability band
  (`lo`/`hi`/`prob`), expiry, and — once graded — `verdict`, realized price,
  and resolve time.
- `get_registry` — the full registry document (data contract v1).

Registry metadata carries the data attribution (market reference data from
sources such as CoinGecko) and the explicit note that perp funding and open
interest are internal analysis inputs and are **not** part of this dataset.

## Transport

stdio JSON-RPC 2.0, one JSON object per line (ndjson). Supported methods:
`initialize`, `ping`, `tools/list`, `tools/call`.

Quick shell smoke test:

```sh
printf '%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_registry_summary","arguments":{}}}' \
  | python3 server/mcp_server.py
```

A client session:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"example","version":"0.1.0"}}}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_horizon_calls","arguments":{"horizon":"24h","status":"resolved"}}}
```

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `FORECASTLEDGER_REGISTRY_PATH` | `/root/business/registry/registry.json` | Path to the registry document to serve |

Point the variable at any registry JSON conforming to the data contract —
including the committed test fixture (`tests/fixtures/registry.json`,
synthetic data). Missing, unreadable, or malformed registries produce
structured, non-fatal errors: tool calls return an error *result*, the
stdio loop survives arbitrary malformed input lines (JSON-RPC `-32700`
parse errors), and unknown methods get `-32601`.

Zero runtime dependencies — Python 3.10+ standard library only.

## Data contract (v1)

```json
{
  "generated_ts": 1791676800,
  "registry_start": "2026-10-01",
  "horizons": {
    "24h": { "calls": [ { "id", "issued_ts", "direction", "prob", "lo", "hi",
                            "expires_ts", "status", "verdict", "realized_px",
                            "resolved_ts" } ],
              "stats": { "n_graded", "hit_rate", "brier", "baseline_brier", "armed" } },
    "3d":  { "..." : "same shape" },
    "7d":  { "..." : "same shape" },
    "4w":  { "..." : "same shape" }
  },
  "meta": { "attribution", "framing", "exchange_derived_note" }
}
```

`status` is `resolved | open`; `verdict` (resolved calls only) is
`inside | above | below`. The grader engine that *produces* this document is
a separate component; see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Repo layout

```
server/mcp_server.py   stdio JSON-RPC MCP server (stdlib only)
server.json            MCP registry manifest (publisher manifest, v0.1.0)
tests/                 unittest/pytest suite + fixture registry
docs/ARCHITECTURE.md   system overview
.github/workflows/     CI (pytest on push/PR)
```

## Status

Pre-data: the registry document is produced by the (in-progress) grader
engine; until then the server is exercised against the synthetic fixture
only. Repository registry submissions (Smithery, Glama, PulseMCP, mcp.so)
and the public ledger site are separate upcoming steps.

## License

MIT — see [LICENSE](LICENSE).