# ForecastLedger — Architecture

ForecastLedger publishes a graded BTC market-forecast registry: every call
is timestamped when issued, graded against outcomes when they resolve, and
published with its calibration statistics — hits and misses alike. The
output is a dataset of critical decision-making data points; it is not
advice and not personalised.

## System overview

```
+------------------------+        +----------------------+        +----------------+
| market reference data  |        | grader engine        |        | registry.json  |
| (e.g. CoinGecko)       +-------> | forecast producer &  +------> | (data contract |
+------------------------+        | call grader          |        |  v1 document)  |
                                  +----------------------+        +-------+--------+
                                                                          |
                                                              reads at runtime
                                                                          v
+------------------------+        +----------------------+        +----------------+
| MCP clients            +-------> | MCP server (stdio    +------> | registry.json  |
| (IDEs, agents, scripts)|        | JSON-RPC 2.0)        |        | source of this |
+------------------------+        +----------------------+        |  repository    |
                                                                          +
                                  +----------------------+        | FORECASTLEDGER |
                                  | public ledger site   |  <---- | REGISTRY_PATH  |
                                  | (static, published   |        +----------------+
                                  |  snapshot of stats)  |
                                  +----------------------+
```

## Components

### Registry producer (grader engine) — *separate repository*

Issues BTC forecasts for four horizons (24h / 3d / 7d / 4w) and grades each
one when its window expires. It maintains the registry document and its
per-horizon statistics. Key properties:

- **Write-once records**: calls are appended with their issue timestamp;
  verdicts are computed at resolution time. No backdating, no silent edits.
- **Calibration stats**: `n_graded`, `hit_rate`, `brier` per horizon;
  `baseline_brier` is the published naive baseline (0.25). A horizon's
  stats carry an `armed` flag — below its minimum sample count the stats
  are shown but not claimed as evidence.
- **Derived-artifacts-only dataset**: market reference data comes from
  public sources (attribution recorded in the registry metadata, e.g.
  CoinGecko). Exchange-internal signals the producer may use for its own
  analysis — such as perp funding and open interest — are **never served**:
  they do not appear in any registry field, API response, or export.

### MCP server — *this repository*

Stdio JSON-RPC 2.0 transport (one JSON object per line), three methods of
the MCP surface (`initialize`, `tools/list`, `tools/call`) plus `ping`,
three read-only tools over the registry document, zero third-party
dependencies. The server is a stateless reader: it opens the registry file
per call, applies the status filter or headline projection, and returns
structured JSON. Failures are structured and non-fatal — missing/malformed
registry, invalid parameters, unknown methods, and malformed input lines
all produce error responses while the stdio loop stays alive.

### Public ledger site — *separate repository*

A static, regenerated snapshot of the registry (headline stats and graded
history) for non-MCP readers, including anyone who wants to audit
calibration without installing a client.

## Data contract

`registry.json` (data contract v1) is the single interface between
producer and consumers:

| Field | Meaning |
|-------|---------|
| `generated_ts` | Unix seconds when the document was regenerated |
| `registry_start` | Date the registry began |
| `horizons` | Map keyed by `24h`, `3d`, `7d`, `4w` |
| `horizons.H.calls[]` | Graded calls (id, issued/expiry timestamps, direction, probability band, status, verdict, realized price, resolve time) |
| `horizons.H.stats` | Calibration summary (n_graded, hit_rate, brier, baseline_brier, armed) |
| `meta` | Attribution, framing line, and the exchange-derived-data note |

`status`: `resolved | open`. `verdict` (resolved only): `inside | above |
below`. The committed test fixture in this repo is synthetic data that
exercises the full contract shape.

## Attribution & scope

- Market reference data: public sources such as CoinGecko; exact sources
  are recorded in `meta.attribution` of every registry document.
- The dataset contains forecast records and outcomes only. Perp funding and
  open interest remain internal analysis inputs and are never published as
  part of the dataset.
- The server handles no credentials, writes nothing, and contacts nothing:
  it reads one local file and answers stdio requests.

## Trust posture

- The public ledger and this server publish *graded history*, including
  misses; calibration honesty is the product.
- Un-armed horizons (insufficient graded sample) are labelled, not hidden.