# CLAUDE.md — igepn-mcp

> Design rationale: the *why*, so a future session doesn't undo a deliberate choice.
> **Twin of `football-mcp`** (structured source → SQLite → typed MCP tools) — this is the **local
> seismic/volcanic** specialization. Unlike `news-mcp` (rolling window of RSS text), this **accumulates** — the
> store is a growing geophysical catalog, a historical asset.
> This repo is **code only** (generic and public). Deployment details live outside it — see "Deployment split".

## What this is
A **stateful, SQL-backed MCP server** giving a small local LLM (a ~12B home assistant, so context is precious)
**current and historical Ecuadorian earthquake and volcano data**, sourced from the **IGEPN public Telegram
channel** and served from local SQLite that a **3-minute poller** refreshes. Two parts: a **poller** (fetch →
classify → parse → append) and an **MCP query server** (reads SQLite, typed tools). Python + `mcp` + `httpx` +
stdlib `sqlite3`.

**IGEPN** = Instituto Geofísico de la Escuela Politécnica Nacional — Ecuador's national seismic/volcanic monitoring
authority (since 1983). Its denser local network catches small local quakes the global nets miss, reports in Spanish
with **human place names**, and — uniquely — publishes **volcano activity levels** (USGS has no equivalent).

## The star query
**"¿qué fue ese temblor hace unos minutos?"** — answered from the freshest IGEPN report: magnitude, depth,
location ("a 25 km de Cuenca"), and whether it's preliminary or revised. That single question is the reason this
exists; everything else (volcano status, historical analysis) is upside on the same store.

## Where it fits
Grounding tiers by freshness + type:
- **openzim** (offline Wikipedia) → deep / historical, static snapshot.
- **news-mcp** → curated current events, RSS text, rolling window.
- **football-mcp** → structured sport, accumulates.
- **igepn-mcp (this)** → **structured local seismic/volcanic, accumulates.**
- **web_search / USGS** → broad + global fallback (see "USGS" below).

## Source — the IGEPN public Telegram channel
**`https://t.me/s/SismosVolcanesIGEPN`** — "Sismos & Volcanes - IGEPN".
- **Transport = the public `t.me/s/` preview page (keyless).** It's Telegram's own public, indexable preview served
  to any honest client — with-the-grain: not a bot-block bypass, no UA spoofing, no account, no secret.
- **`?before=<id>` pages back to the channel's first post (2019)**, ~20 posts a page, verified live. So the preview
  is not just a recent window: it's both the live feed and the archive.
- **Upgrade path (only if the preview ever truncates or gets flaky):** the official MTProto client API
  (Telethon/GramJS + a free API ID/hash, using a dedicated throwaway account). Not needed today.
- Be a polite poller: honest `User-Agent`; a down fetch = skip the run, don't crash.

## Post taxonomy — classify, then keep or drop
| Class | Marker | Action |
|---|---|---|
| **Earthquake** | starts `[PRELIMINAR]` / `[REVISADO]` / `[CONFIRMADO]` + has `Evento:` / `Código:` | parse → `quakes` |
| **Volcano report** | `Informe Diario\|Semanal\|Mensual #<Volcán>` | parse → `volcano_reports` |
| **Instant alert** | `#IGAlInstante` (lahar / ash / activity) | keep text → `alerts` (`kind='instante'`) |
| **Special report** | `INFORME VOLCÁNICO ESPECIAL` | keep text → `alerts` (`kind='especial'`) |
| **Procurement** | `Requerimiento de Información` | **DROP** (not stored at all) |
| **Outreach** | `#VuelosyVolcanesIG` / `#ComunidadIG` | raw only |
| **Other** | photo-only posts, `#Condolencias`, ... | raw only |

### Earthquake post — current format (2023+)
```
[PRELIMINAR]
Evento: igepn2026rymx
Ocurrido: 2026-09-13 02:53:03
Mag.: 3.8M
Prof.: 10.0 km
Lat.: 3.127° S
Long.: 79.066° W
Localizado: a 25.91 km de Cuenca, Azuay
Sintió este sismo? Repórtelo: https://servicios.igepn.edu.ec/url/...
```
- **`Ocurrido` is local time, UTC−5** — verified: post 12357 was published at 13:49Z for an event at 08:46 local.
  Stored as UTC; tools return both `occurred_local_ec` and `occurred_utc` so the small model never does timezone math.
- S/W → negative decimal. `3.8M` / `3.3MLv` → number + type.
- **Format drift (all parsed):** 2021 says `[CONFIRMADO]` instead of `[REVISADO]`; 2019 uses `Código:` /
  `Tiempo Local:` / `Mag.: 3.8 (MLv)` / `Zona:` / `Localización: lat lon` / `Prof.: 8.5(km)` on one line.
- A REVISADO can arrive with no PRELIMINAR (small quakes), and revisions can move an event a lot (12385→12386:
  Naranjal, Guayas M3.6 → Tumbes, Perú M4.0). So `last_quake` returns the `preliminary` estimate alongside.
- **There is no "felt" flag** (every post has the report-if-felt link), so `latest_quakes` has no `felt_only`; it has
  an accent-insensitive `place` filter instead.

### Volcano report — the format
```
Informe Diario #ElReventador N° 2026-266
miércoles 23 de septiembre de 2026
Nivel de Actividad:
Superficial: Alta
Tendencia Superficial: Sin cambio
Interna: Moderada
Tendencia Interna: Sin cambio
Revisarlo en: https://servicios.igepn.edu.ec/url/...
```
Levels ∈ {Baja, Moderada, Alta, ...}; trends ∈ {Ascendente, Descendente, Sin cambio}. `#ElReventador` and `VOLCÁN EL
REVENTADOR` both normalize to the display name `El Reventador`.

## Refresh — watermark-incremental, every 3 minutes
- Each post carries a stable **message id** (`data-post="SismosVolcanesIGEPN/<id>"`). Store the max seen (the
  watermark). Each poll fetches the newest page and pages back with `?before=` only while there's a gap above the
  watermark (capped; a gap past the cap is logged, and `backfill` fills it). ~480 fetches/day.
- **Two keys, two jobs:** message-id watermark = "what's new"; **`Evento` id = "what's corrected"** — a `[REVISADO]`
  arrives as a *new* message carrying the same `Evento`, and `v_quake_current` reflects the correction.
- Edge (noted, not solved): an in-place edit of a post below the watermark isn't seen. IGEPN corrects with a new post,
  so this is a non-issue in practice.
- **`backfill`** walks history from the oldest post seen: resumable, 3 s per page, ~620 pages for the whole channel.
  It's an explicit command, never automatic. **Not Nitter** (wrong platform, and the kind of bypass this project avoids).

## SQLite schema — append-only raw + derived tables (ACCUMULATE)
The data is golden for later analysis (coords, depth, magnitude, revision timing) → keep everything; never prune.
- **`posts_raw`** — append-only source of truth: every kept post as captured. Only `class` may be recomputed.
- **`quakes`** — **keyed by `msg_id`: one row per report**, not per (evento, status), because an event can get more
  than one report. Preliminary and revised both survive (revision delta / latency is itself a dataset).
- **`v_quake_current`** — latest report per `evento_id`; a revised report wins over the preliminary.
- **`volcano_reports`** — daily, weekly and monthly reports (`report_kind`); history kept for trends.
- **`alerts`** — free-text bulletins (`kind` = `instante` | `especial`).
- **`meta`** — watermark, backfill cursor, last poll health.

Parsed tables are **derived** and rebuildable: `igepn-mcp reparse` regenerates them from `posts_raw` after a parser
fix. So a post that fails to parse is kept raw and logged, never lost.

## MCP tools — STRUCTURED (not text-to-SQL). Good docstrings = the model's decision boundary.
**Family tier (built):** `last_quake()`, `latest_quakes(hours=24, min_mag=None, place=None, n=15)`,
`volcano_status(volcano=None)`, `ig_alerts(hours=48, volcano=None, n=10)`.

**Analytical tier (later — same store):**
- `quakes_near(lat, lon, radius_km, days=30)` — spatial query (haversine).
- `seismicity_stats(days=30, bbox=None)` — counts, magnitude distribution, rate vs. a rolling baseline (swarm hint).
- `volcano_trend(volcano, days=30)` — level/trend history for a volcano.
- `revision_stats(days=90)` — PRELIMINAR→REVISADO magnitude/depth deltas + revision latency (the meta-dataset).

Compact JSON (one text block, `ensure_ascii=False`) to spare a small model's context.

### Metadata
Every tool declares a `title` and all four hints. All are read-only, non-destructive, idempotent and
**closed-world**: they read only the local store; the poller, not a tool, touches the web. A test pins this on the
wire JSON, so a new tool without metadata fails.

### Result contract — and why it's stricter than "return what you found"
Every result has a top-level `status`: `ok` / `refused` (`kind`, `reason`, `advice`) / `error` (`kind`, `detail`).
This server answers safety-adjacent questions, so **"couldn't check" must never read as "all quiet"**:
- **Never-polled store → `error` `no_data`**, on every tool. `last_quake` with posts but no quake yet is also `no_data`.
- **Unknown volcano name → `refused`** with `known_volcanoes`, so a typo doesn't look like "no alerts for Sangay".
- **`ok` with `count: 0` really means nothing was reported**, and every `ok` carries `data.last_check_utc` plus a
  `warning` if the last successful poll is over 20 min old.
- **Faults → `error`** (`database`, `internal`) via `_guarded`, never an exception.
- `n` above 50 is capped; resolved parameters are echoed so a result is reproducible. User text is never rewritten.

The quake's own label is **`report_status`**, deliberately not `status`, so it can't be confused with the result's.

**Known limit:** wrong-type arguments (`hours="abc"`) are rejected by the MCP SDK's validation before the handler
runs, so they surface as a protocol tool error, not `refused`. Accepted: the alternative is untyped parameters, which
would lose the schema the model relies on. Numeric strings (`"24"`) are coerced and work.

## Images — link route (human), not vision-input (deferred)
1. **image → model** (base64 `ImageContent`): fragile through proxies and costly in context. **Deferred.**
2. **visual → human** (return a URL; the UI renders it): reliable, zero model-context cost.

So: results carry `post_url`, the stable Telegram post with its image. `posts_raw.media_url` keeps the CDN link, but
it's signed and may expire, so tools don't return it. Never attach images to a list tool.

## USGS / EMSC — the global backdrop (complementary, NOT this server)
IGEPN is the **local query authority**. **USGS FDSN** and **EMSC** are the fastest structured global feeds, but they
belong to a global-context tier, not here. IGEPN's Spanish local report ("a 25 km de Cuenca") wins for family use.

## Principles / ops
- **No API keys.** Polite poller (honest UA). A down fetch = skip, don't crash.
- **Accumulate, never prune.** Live PRELIMINAR→REVISADO capture timing (`fetched_at`) can't be recovered later.
- **Transport:** stdio for dev; streamable-http + bearer behind an MCP proxy. No OAuth in the server.
- **Faithful presentation:** report only what the source says, keep the preliminary/revised distinction, attribute to
  IGEPN with date and time.
- **TLS via `truststore`** (OS trust store). Only a dev machine behind a TLS-inspecting proxy needs it (certifi, curl
  and uv reject certs there; `uv sync --system-certs`). It's harmless elsewhere: it reads the container's normal CA
  bundle.

## Deployment split
Code here. Compose, network slots, hostnames and proxy wiring live in the private infrastructure repo, **not in this
one** (this repo is public). Keep it that way: no hostnames, IPs or deployment paths in code, tests or docs. The
README's compose and proxy examples stay generic.

## License
**MPL-2.0** (matching the other public MCP repos — file-level weak copyleft: improvements to these files flow back,
commercial-friendly).

## Status
- **Built:** poller (`poll` / `backfill` / `reparse`), MCP server with the family tools, stdio + streamable-http +
  bearer, Dockerfile, offline tests (fixture = real posts).
- **Next:** analytical tools, once data has accumulated.
