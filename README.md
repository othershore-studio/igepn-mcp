# igepn-mcp

Ecuador earthquakes and volcano activity from the **IGEPN** (Instituto Geofísico de la Escuela Politécnica
Nacional) → SQLite (**accumulating**, never pruned) → structured MCP tools.
Source: the IGEPN public Telegram channel, read through its keyless public preview `t.me/s/SismosVolcanesIGEPN`.
Design rationale: [CLAUDE.md](CLAUDE.md).

## Tools
| tool | use |
|---|---|
| `last_quake()` | the most recent quake (revised value if available, plus the preliminary estimate) — *"¿qué fue ese temblor?"* |
| `latest_quakes(hours=24, min_mag?, place?, n=15)` | recent quakes, one per event; `place` is accent-insensitive (`"Manabí"`, `"Quito"`) |
| `volcano_status(volcano?)` | latest surface/internal activity level + trend (all volcanoes reported in the last 30 days, or one) |
| `ig_alerts(hours=48, volcano?, n=10)` | `#IGAlInstante` bulletins (lahars, ash, activity) and special volcano reports, as written |

Every tool declares a `title` and all four MCP hints explicitly. All four tools only read the local store,
so each is read-only, non-destructive, idempotent and closed-world (`openWorldHint=false`); the poller, not a
tool, is what fetches from Telegram. A test enforces this, so a new tool without metadata fails CI.

Times come in Ecuador local time (`occurred_local_ec`, UTC−5) and UTC. Quakes keep IGEPN's label as
`report_status` (`PRELIMINAR` / `REVISADO`, older posts `CONFIRMADO`). Every item carries a `post_url` (the
Telegram post, with its image) for the UI to show.

### Results
Every result has a top-level `status`, so callers never have to interpret prose:

| `status` | meaning | fields |
|---|---|---|
| `ok` | the data is good; `count: 0` really means nothing was reported | the data, the resolved parameters, and `data` (`last_check_utc`, plus `warning` if the last successful poll is over 20 min old) |
| `refused` | the request needs changing: an impossible `hours` / `n`, or a volcano with nothing stored | `kind`, `reason`, `advice` (and `known_volcanoes` for an unknown name) |
| `error` | a fault, or nothing captured yet | `kind` (`no_data`, `database`, `internal`), `detail` |

A store that has never been polled is an `error`, not an empty list, so "couldn't check" can't read as "all quiet".
Arguments of the wrong type (`hours="abc"`) are rejected by the MCP SDK before the tool runs, so those still
surface as a protocol-level tool error.

## Run
```bash
uv sync
uv run igepn-mcp poll                        # fetch new posts once (watermark-incremental; run every ~3 min)
uv run igepn-mcp backfill --pages 50         # walk history backwards, ~20 posts/page, resumable (see below)
uv run igepn-mcp reparse                     # rebuild parsed tables from the raw log after a parser change
uv run igepn-mcp serve                       # stdio (Claude Desktop / dev)
IGEPN_MCP_TOKEN=secret uv run igepn-mcp serve --transport http --host 0.0.0.0 --port 8000   # streamable-http at /mcp
uv run pytest
```

| env | default | |
|---|---|---|
| `IGEPN_DB` | `data/igepn.db` | SQLite path (WAL; poller and server can share it) |
| `IGEPN_POLL_MINUTES` | `0` (off) | >0: `serve` also polls in the background (no timer needed); `3` recommended |
| `IGEPN_MCP_TOKEN` | unset | bearer token required on HTTP (`/healthz` stays open) |
| `IGEPN_CHANNEL` | `SismosVolcanesIGEPN` | Telegram channel |
| `IGEPN_MAX_CATCHUP_PAGES` | `25` | pages one poll may walk back to reach the watermark after downtime |
| `IGEPN_USER_AGENT`, `IGEPN_FETCH_TIMEOUT` | honest UA, `20` | |

### Storage
`posts_raw` is the append-only record of every kept post (procurement notices are dropped). `quakes` keeps
**every** report (preliminary and revised) and the view `v_quake_current` gives the latest per event (the
revised one wins). `volcano_reports` covers daily, weekly and monthly reports, and `alerts` holds the free-text
bulletins. The parsed tables are derived from `posts_raw`, so `reparse` can rebuild them at any time.

### History
The preview pages back to the channel's first post (2019), so `backfill` can recover the full archive through
the same keyless route, about 620 pages. Its default of 3 s between pages keeps it polite. It is resumable, so
it can run in chunks (`--pages 100` at a time). The parser handles all three post formats the channel has used
(2019, 2021, 2023+).

## Claude Desktop
`%APPDATA%\Claude\claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "igepn": {
      "command": "uv",
      "args": ["--directory", "C:\\Code\\igepn-mcp", "run", "igepn-mcp", "serve"],
      "env": { "IGEPN_DB": "C:\\Code\\igepn-mcp\\data\\igepn.db", "IGEPN_POLL_MINUTES": "3" }
    }
  }
}
```

## Docker Compose (e.g. mcpo → Open WebUI)
Build the image with `docker build -t igepn-mcp .`. It serves HTTP on :8000 at `/mcp`, and the healthcheck uses
the open `/healthz` endpoint.
```yaml
services:
  igepn-mcp:
    image: igepn-mcp:latest
    restart: unless-stopped
    environment:
      IGEPN_MCP_TOKEN: ${IGEPN_MCP_TOKEN}    # put it in .env; clients send "Authorization: Bearer <token>"
      IGEPN_POLL_MINUTES: "3"                # poll at startup, then every 3 min (no timer needed)
      IGEPN_USER_AGENT: "igepn-mcp/0.1 (+https://example.org/your-contact)"   # identify your deployment
    volumes:
      - igepn-data:/data                     # SQLite; local disk, not NFS/SMB. Accumulates - back it up.
volumes:
  igepn-data:
```
One-time history backfill into the same volume: `docker compose run --rm igepn-mcp backfill --pages 700`.

### Auth scope
The server is deliberately auth-agnostic beyond an optional bearer token: run it over stdio, or HTTP + bearer on a
private network. It has no OAuth and won't grow one. To publish it more widely (at work, on the internet), put a
management layer in front of it, such as an MCP gateway or an auth proxy that handles OAuth, users and audit.

mcpo entry (from a container on the same network):
```json
{ "mcpServers": { "igepn": { "type": "streamable-http", "url": "http://igepn-mcp:8000/mcp",
  "headers": { "Authorization": "Bearer ${IGEPN_MCP_TOKEN}" } } } }
```

## AI assistance
igepn-mcp is developed openly with the help of Claude (Anthropic). We state this plainly: commits
Claude helped write carry a `Co-Authored-By: Claude` trailer. The code and design are open source so the
work can be inspected, reused, and given back.

## License
Code: [MPL-2.0](LICENSE). The earthquake and volcano reports belong to the **IGEPN**
([igepn.edu.ec](https://www.igepn.edu.ec)); they are fetched from its public channel and stay in your local
database. Attribute them to the IGEPN, keep polling polite (≥3 min), and set `IGEPN_USER_AGENT` to identify
your own deployment. For official information and emergencies, follow the IGEPN and Ecuador's risk-management
authorities directly.
