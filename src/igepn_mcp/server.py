"""MCP query server: structured, read-only tools over the SQLite IGEPN store."""

from __future__ import annotations

import functools
import hmac
import json
import logging
import math
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .config import Settings
from .db import connect, fold, get_meta, vkey
from .parse import EC_OFFSET
from .telegram import POST_URL

log = logging.getLogger("igepn_mcp.server")

MAX_ITEMS = 50
MAX_HOURS = 24 * 366 * 100  # far beyond the channel's history; only here to reject nonsense windows
STATUS_WINDOW_DAYS = 30  # volcano_status() without a name lists volcanoes reported this recently
STALE_AFTER = timedelta(minutes=20)  # poll runs every ~3 min; older than this, say so

INSTRUCTIONS = """\
Ecuador earthquakes and volcano activity from the IGEPN (Instituto Geofísico de la Escuela Politécnica
Nacional, Ecuador's official seismic/volcanic monitoring authority), captured from its public Telegram channel
every few minutes and kept as a growing history. Use it for "what was that tremor / temblor a few minutes ago",
"were there earthquakes in Ecuador today", "how is the Sangay / El Reventador volcano", "any lahar or ash alerts".
It only covers what IGEPN reports (mostly Ecuador and border areas); for worldwide quakes use other tools.

Every result has a top-level `status`. "ok": the data is good (a count of 0 really means nothing was reported;
heed `data.warning` if present). "refused": the request needs changing - follow `advice`. "error": the store is
unavailable or holds no data yet (`kind`, `detail`) - tell the user you could not check; never present this as
"nothing happened".

Report ONLY what the data says, and always attribute it to the IGEPN with the date and time. Times are given
in Ecuador local time (`occurred_local_ec`, UTC-5) and UTC. An earthquake `report_status` of PRELIMINAR means a
first automatic estimate that is usually revised within minutes; REVISADO (older posts: CONFIRMADO) is the
reviewed value - say which one you are giving. Magnitude, depth and location can change a lot between the two.
Volcano reports give two activity levels - `superficial` (surface: emissions, explosions, ash) and `interna`
(internal: seismicity, deformation) - each with a trend (Ascendente / Descendente / Sin cambio). Levels are
IGEPN's own words (Baja, Moderada, Alta, ...); do not translate them into official alert colours. Give the
`url` / `post_url` when the user wants the full report or the image.
"""


# Explicit metadata on every tool (MCP clients assume the worst - destructive, open-world - when hints are omitted).
# The title goes in both places the spec has carried it: Tool.title, and annotations.title for older clients.
# Every tool here only reads this server's own SQLite store (the poller, not a tool, is what touches the web),
# so all are read-only, non-destructive, idempotent and closed-world.
def _read_hints(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                           openWorldHint=False)


def _out(obj: dict[str, Any]) -> str:
    """One compact JSON text block (no escaped Spanish accents - tokens matter for small local models)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# Result contract: callers branch on `status`, never on prose. A refusal is an answer, not an exception, so a
# correct block, a false block and a crash can't all look like "Error executing tool".
def _ok(**fields: Any) -> str:
    return _out({"status": "ok", "source": "IGEPN", **fields})


def _refused(reason: str, advice: str, **extra: Any) -> str:
    return _out({"status": "refused", "kind": "input", "reason": reason, "advice": advice, **extra})


def _error(kind: str, detail: str, **extra: Any) -> str:
    return _out({"status": "error", "kind": kind, "detail": detail, **extra})


def _guarded(tool: Callable[..., str]) -> Callable[..., str]:
    """Turn any fault into an `error` result. functools.wraps keeps the signature the MCP schema is built from."""

    @functools.wraps(tool)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return tool(*args, **kwargs)
        except (sqlite3.Error, OSError) as exc:
            log.exception("%s: store unavailable", tool.__name__)
            return _error("database", f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            log.exception("%s failed", tool.__name__)
            return _error("internal", f"{type(exc).__name__}: {exc}")

    return wrapper


def _parse_utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _since(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(then: datetime, now: datetime) -> str:
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 0:
        return "0 min"
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h {minutes % 60} min"
    return f"{minutes // 1440} d"


def _local(then: datetime) -> str:
    return (then + EC_OFFSET).strftime("%Y-%m-%d %H:%M:%S")


def _like_escape(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _bad_window(hours: float, n: int) -> str | None:
    """A refusal if the look-back window or item count makes no sense (n above MAX_ITEMS is capped and echoed)."""
    if not 0 < hours <= MAX_HOURS:  # also catches NaN
        return _refused(f"hours must be between 0 and {MAX_HOURS}, got {hours}", "use e.g. hours=24 or hours=168")
    if n < 1:
        return _refused(f"n must be at least 1, got {n}", f"use n between 1 and {MAX_ITEMS}")
    return None


def _bad_volcano_name(volcano: str | None) -> str | None:
    if volcano and volcano.strip() and not vkey(volcano):
        return _refused(f"{volcano!r} is not a volcano name", 'use a name like "Sangay" or "Cotopaxi", or omit it')
    return None


def _known_volcanoes(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("""SELECT volcano FROM volcano_reports
                           UNION SELECT volcano FROM alerts WHERE volcano IS NOT NULL ORDER BY 1""")
    return [r["volcano"] for r in rows]


def _unknown_volcano(volcano: str, known: list[str]) -> str | None:
    """A refusal if nothing stored matches this name: a typo must not read as "no alerts for that volcano"."""
    if any(vkey(volcano) in vkey(k) for k in known):
        return None
    return _refused(f"no IGEPN reports or bulletins stored for {volcano!r}",
                    "check the spelling or use one of known_volcanoes; IGEPN may simply not be reporting on it",
                    known_volcanoes=known)


def build_server(settings: Settings) -> MCPServer:
    mcp = MCPServer(name="igepn", instructions=INSTRUCTIONS)
    post_url = lambda msg_id: POST_URL.format(channel=settings.channel, msg_id=msg_id)  # noqa: E731

    def db() -> closing[sqlite3.Connection]:
        return closing(connect(settings.db_path))

    def freshness(conn: sqlite3.Connection) -> dict[str, Any]:
        ok = get_meta(conn, "last_poll_ok_utc")
        info: dict[str, Any] = {"last_check_utc": ok}
        if ok is None or datetime.now(UTC) - _parse_utc(ok) > STALE_AFTER:
            info["warning"] = "data may be out of date: the IGEPN channel has not been checked recently"
        return info

    def store_empty(conn: sqlite3.Connection) -> str | None:
        """An `error` if nothing was ever captured: an empty answer here would falsely read as "all quiet"."""
        if conn.execute("SELECT 1 FROM posts_raw LIMIT 1").fetchone() is None:
            return _error("no_data", "no IGEPN posts captured yet: the poller has not stored anything",
                          data=freshness(conn))
        return None

    def quake(row: sqlite3.Row, now: datetime) -> dict[str, Any]:
        occurred = _parse_utc(row["occurred_utc"])
        return {
            "evento": row["evento_id"],
            "report_status": row["status"],
            "occurred_local_ec": _local(occurred),
            "occurred_utc": row["occurred_utc"],
            "ago": _ago(occurred, now),
            "mag": row["mag"],
            "mag_type": row["mag_type"],
            "depth_km": row["depth_km"],
            "lat": row["lat"],
            "lon": row["lon"],
            "place": row["place"],
            "felt_report_url": row["felt_url"],
            "post_url": post_url(row["msg_id"]),
        }

    @mcp.tool(title="Last earthquake", annotations=_read_hints("Last earthquake"), structured_output=False)
    @_guarded
    def last_quake() -> str:
        """The single most recent earthquake reported by the IGEPN, with its latest (revised if available)
        values. Use for "¿qué fue ese temblor?", "what was that earthquake just now", "did it just shake?".

        Check `ago`: if the last quake is hours or days old, the tremor the user felt may not be reported yet
        (a PRELIMINAR report usually appears within ~5 minutes) - say so rather than guessing.
        When the event was revised, `preliminary` shows the first estimate for comparison.
        """
        now = datetime.now(UTC)
        with db() as conn:
            row = conn.execute("SELECT * FROM v_quake_current ORDER BY occurred_utc DESC LIMIT 1").fetchone()
            fresh = freshness(conn)
            if row is None:
                return _error("no_data", "no earthquake captured yet, so the latest one is unknown", data=fresh)
            prelim = None
            if row["status"] != "PRELIMINAR":
                prelim = conn.execute(
                    "SELECT * FROM quakes WHERE evento_id=? AND status='PRELIMINAR' ORDER BY msg_id LIMIT 1",
                    (row["evento_id"],),
                ).fetchone()
        item = quake(row, now)
        if prelim is not None:
            item["preliminary"] = {
                "mag": prelim["mag"], "mag_type": prelim["mag_type"], "depth_km": prelim["depth_km"],
                "place": prelim["place"], "posted_utc": prelim["posted_at"],
            }
        return _ok(quake=item, data=fresh)

    @mcp.tool(title="Latest earthquakes", annotations=_read_hints("Latest earthquakes"), structured_output=False)
    @_guarded
    def latest_quakes(hours: float = 24, min_mag: float | None = None, place: str | None = None, n: int = 15) -> str:
        """Recent earthquakes reported by the IGEPN, newest first, one entry per event (its latest report).
        Use for "earthquakes today / this week", "any tremors near Quito?", "strong quakes this month".

        Args:
            hours: look-back window in hours (default 24; e.g. 168 = one week, 720 = 30 days).
            min_mag: only events with magnitude >= this (e.g. 4).
            place: text that must appear in the location, case/accent-insensitive: a city or province as IGEPN
                writes it, e.g. "Quito", "Manabí", "Esmeraldas" (IGEPN names the NEAREST town, so a quake felt
                in a city may be listed under a neighbouring one).
            n: max events (default 15, max 50).
        """
        if refusal := _bad_window(hours, n):
            return refusal
        if min_mag is not None and not math.isfinite(min_mag):
            return _refused(f"min_mag must be a number, got {min_mag}", "use e.g. min_mag=4, or omit it")
        now = datetime.now(UTC)
        limit = min(n, MAX_ITEMS)
        where, params = ["occurred_utc >= ?"], [_since(hours)]
        if min_mag is not None:
            where.append("mag >= ?")
            params.append(min_mag)
        if place and place.strip():
            where.append("fold(place) LIKE ? ESCAPE '\\'")
            params.append(_like_escape(fold(place.strip())))
        sql = f"SELECT * FROM v_quake_current WHERE {' AND '.join(where)} ORDER BY occurred_utc DESC LIMIT ?"
        with db() as conn:
            if failure := store_empty(conn):
                return failure
            rows = conn.execute(sql, [*params, limit]).fetchall()
            fresh = freshness(conn)
        return _ok(hours=hours, min_mag=min_mag, place=place, n=limit, count=len(rows),
                   quakes=[quake(r, now) for r in rows], data=fresh)

    @mcp.tool(title="Volcano status", annotations=_read_hints("Volcano status"), structured_output=False)
    @_guarded
    def volcano_status(volcano: str | None = None) -> str:
        """Latest IGEPN activity report for Ecuadorian volcanoes: surface and internal activity level, each
        with its trend. Use for "how is the Sangay", "is Cotopaxi active", "volcano status".

        Without `volcano`: every volcano with a report in the last 30 days (IGEPN only publishes periodic
        reports for volcanoes it is actively following; others are not listed). With `volcano`: that
        volcano's latest report however old (check `report_date`; null if it only has bulletins) plus its
        recent instant alerts. A name with nothing stored is refused and lists `known_volcanoes`.

        Args:
            volcano: name, case/accent/space-insensitive, e.g. "Sangay", "reventador", "Cotopaxi".
        """
        if refusal := _bad_volcano_name(volcano):
            return refusal
        today = (datetime.now(UTC) + EC_OFFSET).date()

        def report(r: sqlite3.Row) -> dict[str, Any]:
            return {
                "volcano": r["volcano"],
                "report": f"Informe {r['report_kind'].capitalize()} N° {r['report_no']}",
                "report_date": r["report_date"],
                "days_old": (today - datetime.strptime(r["report_date"], "%Y-%m-%d").date()).days,
                "superficial": {"level": r["superficial_level"], "trend": r["superficial_trend"]},
                "interna": {"level": r["interna_level"], "trend": r["interna_trend"]},
                "url": r["url"],
                "post_url": post_url(r["msg_id"]),
            }

        latest_sql = """SELECT * FROM volcano_reports v WHERE v.msg_id = (
                            SELECT v2.msg_id FROM volcano_reports v2 WHERE v2.volcano = v.volcano
                            ORDER BY v2.report_date DESC, v2.msg_id DESC LIMIT 1)"""
        with db() as conn:
            if failure := store_empty(conn):
                return failure
            fresh = freshness(conn)
            if not volcano or not volcano.strip():
                cutoff = (today - timedelta(days=STATUS_WINDOW_DAYS)).isoformat()
                rows = conn.execute(f"{latest_sql} AND v.report_date >= ? ORDER BY v.volcano", (cutoff,)).fetchall()
                return _ok(window_days=STATUS_WINDOW_DAYS, volcanoes=[report(r) for r in rows], data=fresh)

            if refusal := _unknown_volcano(volcano, _known_volcanoes(conn)):
                return refusal
            key = f"%{vkey(volcano)}%"
            row = conn.execute(f"{latest_sql} AND vkey(v.volcano) LIKE ? ORDER BY v.report_date DESC LIMIT 1",
                               (key,)).fetchone()
            alerts = conn.execute(
                "SELECT * FROM alerts WHERE vkey(volcano) LIKE ? AND posted_at >= ? ORDER BY posted_at DESC LIMIT 3",
                (key, _since(24 * 7)),
            ).fetchall()
        return _ok(
            volcano=volcano,
            report=report(row) if row is not None else None,
            alerts_last_7_days=[
                {"posted_utc": a["posted_at"], "posted_local_ec": _local(_parse_utc(a["posted_at"])),
                 "title": a["title"], "url": a["url"]}
                for a in alerts
            ],
            data=fresh,
        )

    @mcp.tool(title="IGEPN alerts", annotations=_read_hints("IGEPN alerts"), structured_output=False)
    @_guarded
    def ig_alerts(hours: float = 48, volcano: str | None = None, n: int = 10) -> str:
        """Recent IGEPN instant bulletins (#IGAlInstante: lahars, ash emissions, increased activity) and special
        volcano reports, newest first, as IGEPN wrote them. Use for "any volcano alerts", "is there ash from
        the Sangay", "lahar warnings". Relay safety recommendations exactly as written.

        Args:
            hours: look-back window in hours (default 48; 168 = one week).
            volcano: optional volcano name filter, e.g. "Sangay". A name with nothing stored is refused and
                lists `known_volcanoes`.
            n: max bulletins (default 10, max 50).
        """
        if refusal := _bad_window(hours, n) or _bad_volcano_name(volcano):
            return refusal
        limit = min(n, MAX_ITEMS)
        where, params = ["posted_at >= ?"], [_since(hours)]
        with db() as conn:
            if failure := store_empty(conn):
                return failure
            if volcano and volcano.strip():
                if refusal := _unknown_volcano(volcano, _known_volcanoes(conn)):
                    return refusal
                where.append("vkey(volcano) LIKE ?")
                params.append(f"%{vkey(volcano)}%")
            sql = f"SELECT * FROM alerts WHERE {' AND '.join(where)} ORDER BY posted_at DESC LIMIT ?"
            rows = conn.execute(sql, [*params, limit]).fetchall()
            fresh = freshness(conn)

        def text(t: str, limit: int = 900) -> str:
            return t if len(t) <= limit else t[:limit].rsplit(" ", 1)[0] + "…"

        return _ok(hours=hours, volcano=volcano, n=limit, count=len(rows), alerts=[
            {"kind": a["kind"], "volcano": a["volcano"], "posted_utc": a["posted_at"],
             "posted_local_ec": _local(_parse_utc(a["posted_at"])), "title": a["title"], "text": text(a["text"]),
             "url": a["url"], "post_url": post_url(a["msg_id"])}
            for a in rows
        ], data=fresh)

    return mcp


class BearerAuth:
    """Minimal ASGI gate for streamable-http: requires `Authorization: Bearer <token>` when a token is set.
    /healthz is always open (the Docker healthcheck uses it, token or not)."""

    def __init__(self, app, token: str | None) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode() if token else None

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            if scope["path"] == "/healthz":
                await _respond(send, 200, b"ok")
                return
            if self.expected is None:
                await self.app(scope, receive, send)
                return
            got = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await _respond(send, 401, b"unauthorized")
                return
        await self.app(scope, receive, send)


async def _respond(send, status: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
