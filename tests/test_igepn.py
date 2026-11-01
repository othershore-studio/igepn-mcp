"""Offline tests: preview parsing, classification, field parsing, polling, and tools (no network).

tests/fixtures/preview_sample.html holds real channel posts (2019, 2021 and 2026 formats) trimmed from the
public t.me/s/SismosVolcanesIGEPN preview.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import igepn_mcp.poll as poll_mod
from igepn_mcp.config import Settings
from igepn_mcp.db import connect, get_meta, set_meta
from igepn_mcp.parse import (
    ALERT, OTHER, OUTREACH, PROCUREMENT, QUAKE, VOLCANO_REPORT, classify, parse_alert, parse_quake,
    parse_volcano_report, volcano_name,
)
from igepn_mcp.poll import poll, reparse, store_posts, StoreStats
from igepn_mcp.server import build_server
from igepn_mcp.telegram import Post, parse_preview

SAMPLE = (Path(__file__).parent / "fixtures" / "preview_sample.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def sample() -> dict[int, Post]:
    return {p.msg_id: p for p in parse_preview(SAMPLE)}


# ---------------------------------------------------------------- transport / HTML

def test_preview_parse(sample):
    assert list(sample) == sorted(sample) and len(sample) == 15
    p = sample[12357]
    assert p.posted_at == "2026-09-06T13:49:02Z"
    assert p.text.splitlines()[:3] == ["[PRELIMINAR]", "Evento: igepn2026rmea", "Ocurrido: 2026-09-06 08:46:18"]
    assert p.media_url and p.media_url.startswith("https://")
    assert sample[12378].text == ""  # photo-only post


def test_classify(sample):
    got = {i: classify(p.text) for i, p in sample.items()}
    assert got[12357] == got[12366] == got[1998] == got[57] == QUAKE
    assert got[12455] == got[12364] == VOLCANO_REPORT
    assert got[12384] == got[12433] == ALERT
    assert got[12436] == PROCUREMENT
    assert got[12447] == OUTREACH
    assert got[12378] == OTHER


# ---------------------------------------------------------------- field parsing

def test_parse_quake_current_format(sample):
    q = parse_quake(sample[12358].text)
    assert (q.evento_id, q.status, q.mag, q.mag_type, q.depth_km) == ("igepn2026rmea", "REVISADO", 3.5, "MLv", 37.0)
    assert q.occurred_utc == "2026-09-06T13:46:31Z"  # 08:46:31 Ecuador local (UTC-5)
    assert (q.lat, q.lon) == (0.61, -80.164)  # N positive, W negative
    assert q.place == "a 15.94 km de Muisne, Esmeraldas"
    assert q.felt_url == "https://servicios.igepn.edu.ec/url/ODQ2Mg=="
    assert parse_quake(sample[12366].text).place == "a 153.49 km de Gualaquiza, Morona Santiago"  # extra lines ignored


def test_parse_quake_old_formats(sample):
    q21 = parse_quake(sample[1998].text)
    assert (q21.status, q21.mag, q21.mag_type, q21.lat) == ("CONFIRMADO", 3.6, "M", -2.174)
    q19 = parse_quake(sample[57].text)
    assert (q19.evento_id, q19.status, q19.occurred_utc) == ("igepn2019smld", "CONFIRMADO", "2019-09-20T22:54:50Z")
    assert (q19.mag, q19.mag_type, q19.depth_km, q19.lat, q19.lon) == (3.8, "MLv", 8.5, 0.928, -79.788)
    assert q19.place == "Near Coast of Ecuador"


def test_parse_volcano_report(sample):
    r = parse_volcano_report(sample[12455].text, sample[12455].posted_at)
    assert (r.volcano, r.report_kind, r.report_no, r.report_date) == ("El Reventador", "diario", "2026-271", "2026-09-28")
    assert (r.superficial_level, r.superficial_trend, r.interna_level, r.interna_trend) == (
        "Alta", "Sin cambio", "Moderada", "Sin cambio")
    assert parse_volcano_report(sample[12364].text, sample[12364].posted_at).report_kind == "mensual"
    # levels and trends on one line, as some clients render them
    one_line = ("Informe Diario #Sangay N° 2026-254\nlunes 28 de septiembre de 2026\nNivel de Actividad:\n"
                "Superficial: Moderada          Tendencia Superficial: Ascendente\n"
                "Interna: Baja          Tendencia Interna: Sin cambio\nRevisarlo en: https://x/y")
    r = parse_volcano_report(one_line, "2026-09-28T16:58:34Z")
    assert (r.superficial_level, r.superficial_trend, r.interna_level) == ("Moderada", "Ascendente", "Baja")


def test_parse_alert_and_names(sample):
    a = parse_alert(sample[12384].text)
    assert (a.kind, a.volcano) == ("instante", "El Reventador")
    assert a.title == "Informativo VOLCÁN EL REVENTADOR No. 2026-030"
    assert parse_alert(sample[12433].text).kind == "especial"
    assert volcano_name("#GuaguaPichincha") == "Guagua Pichincha" == volcano_name("GUAGUA PICHINCHA")


# ---------------------------------------------------------------- poller

def _fake_pages(pages: list[list[Post]]):
    """fetch_page stand-in serving `pages` like Telegram: newest page first, ?before= pages back."""
    all_posts = sorted({p.msg_id: p for page in pages for p in page}.values(), key=lambda p: p.msg_id)
    calls = []

    async def fetch(client, channel, before=None):
        calls.append(before)
        older = [p for p in all_posts if before is None or p.msg_id < before]
        return older[-4:]  # 4 posts per "page"

    return fetch, calls


def test_poll_watermark_and_catchup(tmp_path, sample, monkeypatch):
    settings = Settings(db_path=tmp_path / "igepn.db")
    posts = list(sample.values())
    fetch, calls = _fake_pages([posts[:8]])
    monkeypatch.setattr(poll_mod, "fetch_page", fetch)
    r = asyncio.run(poll(settings))  # first run: newest page only
    assert (r.status, r.pages, r.watermark, calls) == ("ok", 1, 12384, [None])

    fetch, calls = _fake_pages([posts])  # 7 newer posts arrived: needs 2 pages to reach the watermark
    monkeypatch.setattr(poll_mod, "fetch_page", fetch)
    r = asyncio.run(poll(settings))
    assert (r.pages, r.watermark, calls) == (2, 12455, [None, 12436])
    assert r.stats.dropped == 1  # procurement
    with connect(settings.db_path) as conn:
        ids = [row[0] for row in conn.execute("SELECT msg_id FROM posts_raw")]
        assert 12436 not in ids and 12366 in ids and 12384 in ids
        assert conn.execute("SELECT COUNT(*) FROM quakes").fetchone()[0] == 3  # 12366, then 12385 + 12386
        assert get_meta(conn, "last_poll_status") == "ok"

    r = asyncio.run(poll(settings))  # nothing new
    assert (r.pages, r.stats.new) == (1, 0)


def test_poll_down_fetch_is_skipped(tmp_path, monkeypatch):
    import httpx

    async def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(poll_mod, "fetch_page", boom)
    settings = Settings(db_path=tmp_path / "igepn.db")
    r = asyncio.run(poll(settings))
    assert r.status.startswith("error: ConnectError")
    with connect(settings.db_path) as conn:
        assert get_meta(conn, "last_poll_ok_utc") is None


def test_reparse_rebuilds(tmp_path, sample):
    settings = Settings(db_path=tmp_path / "igepn.db")
    conn = connect(settings.db_path)
    store_posts(conn, list(sample.values()), StoreStats())
    before = conn.execute("SELECT * FROM quakes ORDER BY msg_id").fetchall()
    conn.close()
    counts = reparse(settings)
    assert counts[QUAKE] == 7 and counts[VOLCANO_REPORT] == 3 and counts[ALERT] == 2
    assert not any(k.endswith("_unparsed") for k in counts)
    with connect(settings.db_path) as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM quakes ORDER BY msg_id")] == [tuple(r) for r in before]


# ---------------------------------------------------------------- tools (seeded with times relative to now)

NOW = datetime.now(UTC)


def _post(msg_id: int, minutes_ago: float, text: str) -> Post:
    return Post(msg_id, (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ"), text)


def _es_date(dt: datetime) -> str:
    """Today as a report states it, so seeded reports stay inside volcano_status's 30-day window."""
    local = dt - timedelta(hours=5)
    months = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
              "noviembre", "diciembre"]
    return f"{local.day:02d} de {months[local.month - 1]} de {local.year}"


def _quake_text(status, evento, minutes_ago, mag, place, lat="3.127° S"):
    local = (NOW - timedelta(minutes=minutes_ago) - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
    return (f"[{status}]\nEvento: {evento}\nOcurrido: {local}\nMag.: {mag}\nProf.: 10.0 km\nLat.: {lat}\n"
            f"Long.: 79.066° W\nLocalizado: {place}\nSintió este sismo? Repórtelo: https://example/{evento}")


@pytest.fixture
def server(tmp_path):
    settings = Settings(db_path=tmp_path / "igepn.db")
    posts = [
        _post(100, 60 * 30, _quake_text("REVISADO", "old1", 60 * 30, "4.5MLv", "a 5 km de Manta, Manabí")),
        _post(101, 12, _quake_text("PRELIMINAR", "ev1", 15, "3.8M", "a 25.91 km de Cuenca, Azuay")),
        _post(102, 6, _quake_text("REVISADO", "ev1", 15, "3.3MLv", "a 16.22 km de Cuenca, Azuay")),
        _post(103, 3, _quake_text("PRELIMINAR", "ev2", 4, "2.9M", "a 10 km de Quito, Pichincha", "0.2° S")),
        _post(104, 60, f"Informe Diario #Sangay N° 2026-254\n{_es_date(NOW)}\nNivel de Actividad:\n"
                       "Superficial: Moderada\nTendencia Superficial: Ascendente\nInterna: Baja\n"
                       "Tendencia Interna: Sin cambio\nRevisarlo en: https://example/sangay"),
        _post(105, 90, "#IGAlInstante Informativo VOLCÁN SANGAY No. 2026-007\nDesde las 07:08 TL, emisión de "
                       "ceniza hacia el occidente.\nVer informe: https://example/alert"),
    ]
    conn = connect(settings.db_path)
    store_posts(conn, posts, StoreStats())
    set_meta(conn, "last_poll_ok_utc", NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))
    conn.commit()
    conn.close()
    return build_server(settings)


def call(server, name, args):
    result = asyncio.run(server.call_tool(name, args))
    content = result.content if hasattr(result, "content") else result[0]
    return json.loads(content[0].text)


def test_last_quake(server):
    out = call(server, "last_quake", {})
    q = out["quake"]
    assert out["source"] == "IGEPN" and "warning" not in out["data"]
    assert (q["evento"], q["status"], q["mag"], q["lat"]) == ("ev2", "PRELIMINAR", 2.9, -0.2)
    assert q["ago"] == "4 min" and q["post_url"] == "https://t.me/SismosVolcanesIGEPN/103"


def test_latest_quakes_current_view_and_filters(server):
    out = call(server, "latest_quakes", {})
    assert [q["evento"] for q in out["quakes"]] == ["ev2", "ev1"]  # one row per event; old1 outside 24 h
    assert out["quakes"][1]["status"] == "REVISADO" and out["quakes"][1]["mag"] == 3.3  # revision wins
    assert call(server, "latest_quakes", {"min_mag": 3})["count"] == 1
    assert call(server, "latest_quakes", {"hours": 48, "place": "MANABI"})["quakes"][0]["evento"] == "old1"


def test_last_quake_shows_preliminary_when_revised(server, tmp_path):
    conn = connect(tmp_path / "igepn.db")
    conn.execute("DELETE FROM quakes WHERE evento_id='ev2'")
    conn.commit()
    conn.close()
    q = call(server, "last_quake", {})["quake"]
    assert q["evento"] == "ev1" and q["status"] == "REVISADO"
    assert q["preliminary"]["mag"] == 3.8 and q["preliminary"]["place"] == "a 25.91 km de Cuenca, Azuay"


def test_volcano_status_and_alerts(server):
    all_ = call(server, "volcano_status", {})["volcanoes"]
    assert [v["volcano"] for v in all_] == ["Sangay"]
    one = call(server, "volcano_status", {"volcano": "SANGAY "})
    assert one["status"]["superficial"] == {"level": "Moderada", "trend": "Ascendente"}
    assert one["alerts_last_7_days"][0]["url"] == "https://example/alert"
    missing = call(server, "volcano_status", {"volcano": "Cotopaxi"})
    assert missing["found"] is False and missing["volcanoes_with_reports"] == ["Sangay"]
    alerts = call(server, "ig_alerts", {"volcano": "sangay"})
    assert alerts["count"] == 1 and alerts["alerts"][0]["volcano"] == "Sangay"
    assert call(server, "ig_alerts", {"hours": 1})["count"] == 0


def test_stale_data_warning(tmp_path):
    settings = Settings(db_path=tmp_path / "empty.db")
    out = call(build_server(settings), "last_quake", {})
    assert out["found"] is False and "warning" in out["data"]


def test_healthz_open_and_bearer_required():
    from igepn_mcp.server import BearerAuth

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    def status(gate, path, headers=()):
        sent = []

        async def send(msg):
            sent.append(msg)

        asyncio.run(gate({"type": "http", "path": path, "headers": list(headers)}, None, send))
        return sent[0]["status"]

    locked = BearerAuth(app, "s3cret")
    assert status(locked, "/healthz") == 200
    assert status(locked, "/mcp") == 401
    assert status(locked, "/mcp", [(b"authorization", b"Bearer s3cret")]) == 204
    assert status(BearerAuth(app, None), "/mcp") == 204 and status(BearerAuth(app, None), "/healthz") == 200


# ---------------------------------------------------------------- MCP metadata (a new tool without it fails CI)

TOOLS = {"last_quake", "latest_quakes", "volcano_status", "ig_alerts"}


def test_every_tool_declares_title_and_all_four_hints(tmp_path):
    tools = asyncio.run(build_server(Settings(db_path=tmp_path / "igepn.db")).list_tools())
    assert {t.name for t in tools} == TOOLS
    for t in tools:
        wire = t.annotations.model_dump(by_alias=True)  # the camelCase JSON a client/directory actually reads
        for hint in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
            assert isinstance(wire.get(hint), bool), f"{t.name}.{hint} unset"
        assert t.title and wire.get("title") == t.title, f"{t.name} missing title"
        # all tools only read the local store: read-only, non-destructive, idempotent, closed-world
        assert (wire["readOnlyHint"], wire["destructiveHint"], wire["idempotentHint"], wire["openWorldHint"]) == (
            True, False, True, False), t.name


def test_every_tool_is_exercised_by_a_test():
    src = Path(__file__).read_text(encoding="utf-8")
    for name in TOOLS:  # the pattern is built at runtime, so this line can't match itself
        assert f'call(server, "{name}"' in src, f"{name} is never called by a test"
