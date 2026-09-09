"""FastAPI 앱. 실행: uvicorn app.main:app --reload

경로
  GET  /                      프론트 (static/index.html)
  GET  /api/health            상태
  GET  /api/themes            테마·종목 현재 스냅샷
  GET  /api/stocks/{code}     종목 하나
  GET  /api/screener/hoga     고가놀이 결과 (data/hoga.json)
  POST /api/screener/run      일봉을 받아 고가놀이 다시 찾기 (?source=naver|kis|mock&universe=themes|market), 백그라운드
  GET  /api/screener/status   다시 찾기 진행 상황
  GET  /api/schedule          하루 한 번 자동 실행(장 마감 후 스크리너, 장 전 테마 수집) 상태
  POST /api/schedule/run/{name}  자동 실행 작업을 지금 돌리기 (screener | collect)
  GET  /api/overnight         야간 지표 (전일 20:05 대비, 야후 파이낸스에서 주기 수집)
  POST /api/overnight/refresh 야간 지표 즉시 갱신
  POST /api/collect           네이버 테마 수집 → themes.json 갱신 → 즉시 반영
  POST /api/themes/reload     themes.json 을 다시 읽어 반영 (손으로 고쳤을 때)
  POST /api/news/refresh      테마 카드 리포트 줄·뉴스 줄 즉시 갱신 (평소엔 NEWS_MINUTES 주기)
  WS   /ws/stream             1초마다 테마 스냅샷 방송. 테마 목록이 바뀌면 type=snapshot 을 보낸다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from app.collectors import naver_futures
from app.collectors import overnight as overnight_collector
from app.collectors.naver_theme import collect
from app.config import settings
from app.feeds import KisFeed, KiwoomFeed, MockFeed
from app.kis.futures import KisFutures
from app.scheduler import Job, Scheduler
from app.screener.runner import run_screener
from app.state import MarketState

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # 요청 한 줄씩 찍히는 로그는 끈다
log = logging.getLogger("radar")

state = MarketState.from_file(settings.themes_file)
feed: MockFeed | KisFeed | None = None
clients: set[WebSocket] = set()
collect_lock = asyncio.Lock()
last_collect: dict = {"at": None, "themes": 0, "error": None}
overnight_data: dict = {}
overnight_lock = asyncio.Lock()


# ── 피드 · 방송 ────────────────────────────────────────────────
async def start_feed() -> None:
    global feed
    if settings.is_mock:
        feed = MockFeed(state)
    elif settings.broker == "kiwoom":
        feed = KiwoomFeed(state, settings)
    else:
        feed = KisFeed(state, settings)
    log.info("피드: %s (BROKER=%s, %d 테마 %d 종목)", type(feed).__name__, settings.broker, len(state.themes), len(state.stocks))
    await feed.start()


async def stop_feed() -> None:
    global feed
    if feed:
        await feed.stop()
        feed = None


async def broadcast(payload: dict) -> None:
    if not clients:
        return
    msg = json.dumps(payload, ensure_ascii=False)
    dead = []
    for ws in clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


async def broadcaster() -> None:
    while True:
        state.recompute()
        await broadcast(state.snapshot())
        await asyncio.sleep(1.0)


async def reload_state() -> None:
    """themes.json 을 다시 읽고 피드를 다시 붙인 뒤, 클라이언트에 새 테마 목록을 보낸다."""
    global state
    await stop_feed()
    state = MarketState.from_file(settings.themes_file)
    await start_feed()
    state.recompute()
    await broadcast(state.snapshot() | {"type": "snapshot"})


async def run_collect() -> dict:
    if collect_lock.locked():
        raise HTTPException(409, "이미 수집 중입니다.")
    async with collect_lock:
        try:
            data = await collect(top=settings.collect_top, per=settings.collect_per)
            if not data["themes"]:
                raise RuntimeError("수집된 테마가 없습니다 (페이지 구조 변경?)")
            p = settings.themes_file
            if p.exists():
                p.with_suffix(".json.bak").write_text(p.read_text("utf-8"), "utf-8")
            p.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
            await reload_state()
            last_collect.update(at=data["collected_at"], themes=len(data["themes"]), error=None)
            log.info("테마 수집 완료: %d 테마", len(data["themes"]))
            return {"ok": True, "themes": len(data["themes"]), "stocks": len(state.stocks), "at": data["collected_at"],
                    "names": [t["name"] for t in data["themes"]]}
        except HTTPException:
            raise
        except Exception as e:
            last_collect.update(error=str(e))
            log.exception("테마 수집 실패")
            raise HTTPException(502, f"테마 수집 실패: {e}")


def market_hours(now: datetime) -> bool:
    return now.weekday() < 5 and (8, 50) <= (now.hour, now.minute) <= (15, 30)


async def autocollect() -> None:
    minutes = settings.collect_minutes
    log.info("테마 자동 수집: %d분 주기 (장중만)", minutes)
    while True:
        if market_hours(datetime.now()):
            try:
                await run_collect()
            except HTTPException as e:
                log.warning("자동 수집 건너뜀: %s", e.detail)
        await asyncio.sleep(minutes * 60)


async def foreign_futures() -> dict:
    """코스피200 선물 외국인 순매수(계약). 기본은 네이버 투자자별 매매동향(증권사 무관, 키 불필요).
    FUT_SOURCE=kis 면 한국투자증권 API 로 조회. 실패해도 예외 대신 error 를 돌려준다."""
    if settings.fut_source == "kis":
        if not isinstance(feed, KisFeed):
            return {"prev": None, "now": None, "src": "kis", "error": "FUT_SOURCE=kis 인데 BROKER 가 kis 가 아닙니다"}
        r = await KisFutures(feed.rest, settings).foreign_net()
        return {"prev": r.prev, "now": r.today, "src": f"KIS {r.today_date[4:] if r.today_date else ''}".strip(), "error": r.error}
    try:
        rows = await naver_futures.fetch_rows()
        return naver_futures.foreign_summary(rows)
    except Exception as e:
        return {"prev": None, "now": None, "src": "네이버", "error": f"선물 투자자 조회 실패: {e}"}


news_lock = asyncio.Lock()


async def refresh_news() -> dict:
    """테마마다 리포트 줄·뉴스 줄을 새로 만들고 themes.json 에도 써 둔다 (재시작해도 유지)."""
    from app.collectors.naver_news import NaverNews, theme_keywords

    if news_lock.locked():
        raise HTTPException(409, "이미 갱신 중입니다.")
    async with news_lock:
        nn = NaverNews()
        updated = 0
        try:
            for t in state.themes:
                leader = t.leader or t.stocks[0]
                kws = theme_keywords(t.name, [s.why for s in t.stocks])
                try:
                    x = await nn.enrich([(s.code, s.name) for s in t.stocks], leader.code, settings.report_days, keywords=kws)
                except Exception as e:
                    log.warning("뉴스 갱신 실패 %s: %s", t.name, e)
                    continue
                t.report, t.news_updated = x["report"], x["updatedAt"]
                t.news, t.news_url, t.news_at = x["news"], x["newsUrl"], x["newsAt"]
                updated += 1
        finally:
            await nn.close()
        # themes.json 에 반영
        try:
            p = settings.themes_file
            d = json.loads(p.read_text("utf-8"))
            by = {t.id: t for t in state.themes}
            for td in d["themes"]:
                t = by.get(td["id"])
                if t:
                    td.update(report=t.report, news=t.news, newsUrl=t.news_url, newsAt=t.news_at, updatedAt=t.news_updated)
            p.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
        except Exception as e:
            log.warning("themes.json 뉴스 반영 실패: %s", e)
        log.info("리포트·뉴스 갱신: %d/%d 테마", updated, len(state.themes))
        return {"ok": True, "updated": updated, "themes": len(state.themes)}


async def news_loop() -> None:
    minutes = settings.news_minutes
    log.info("리포트·뉴스: %d분 주기", minutes)
    await asyncio.sleep(5)
    while True:
        try:
            await refresh_news()
        except Exception as e:
            log.warning("리포트·뉴스 갱신 실패: %s", e)
        await asyncio.sleep(minutes * 60)


async def refresh_overnight() -> dict:
    """야간 지표를 새로 받아 메모리와 data/overnight.json 에 둔다."""
    async with overnight_lock:
        d = await overnight_collector.collect(extra={"frgn_fut": await foreign_futures()})
        overnight_data.clear()
        overnight_data.update(d)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        (settings.data_dir / "overnight.json").write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
        ok = sum(1 for r in d["rows"] if r["chg"] is not None or r["diff"] is not None)
        log.info("야간 지표 갱신: %d/%d 지표, 기준 %s", ok, len(d["rows"]), d["base_at"])
        return d


async def overnight_loop() -> None:
    minutes = settings.overnight_minutes
    log.info("야간 지표: %d분 주기", minutes)
    while True:
        try:
            await refresh_overnight()
        except Exception as e:
            log.warning("야간 지표 수집 실패: %s", e)
        await asyncio.sleep(minutes * 60)


async def job_screener() -> dict:
    """스케줄용: 증권사 일봉(auto)으로 고가놀이 다시 찾기. /api/screener/status 에도 진행이 보인다."""
    if screener_job["running"]:
        raise RuntimeError("스크리너가 이미 실행 중")
    screener_job.update(running=True, done=0, total=0, current="스케줄 실행", startedAt=datetime.now().isoformat(timespec="seconds"),
                        finishedAt=None, error=None, source="auto", universe=settings.schedule_screener_universe)
    try:
        def progress(i, n, name):
            screener_job.update(done=i, total=n, current=name)

        r = await run_screener("auto", settings.schedule_screener_universe, progress=progress)
        screener_job.update(error=None, rows=r["totalRecent"])
        return {"source": r["source"], "universe": r["universe"], "stocks": r["stocks"], "asof": r["asof"], "rows": r["totalRecent"]}
    except Exception as e:
        screener_job.update(error=str(e))
        raise
    finally:
        screener_job.update(running=False, finishedAt=datetime.now().isoformat(timespec="seconds"))


async def job_collect() -> dict:
    """스케줄용: 테마 재수집(리포트·뉴스 포함) → 반영."""
    r = await run_collect()
    return {"themes": r["themes"], "stocks": r["stocks"]}


scheduler = Scheduler(
    [Job("screener", settings.schedule_screener, job_screener), Job("collect", settings.schedule_collect, job_collect)],
    settings.data_dir / "schedule.json",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate()
    await start_feed()
    tasks = [asyncio.create_task(broadcaster(), name="broadcaster"), asyncio.create_task(scheduler.loop(), name="scheduler")]
    if settings.collect_minutes > 0:
        tasks.append(asyncio.create_task(autocollect(), name="autocollect"))
    if settings.overnight_minutes > 0:
        tasks.append(asyncio.create_task(overnight_loop(), name="overnight"))
    if settings.news_minutes > 0:
        tasks.append(asyncio.create_task(news_loop(), name="news"))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await stop_feed()


app = FastAPI(title="테마 레이더 API", lifespan=lifespan)


# ── 조회 ───────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"ok": True, "broker": settings.broker, "env": settings.kiwoom_env if settings.broker == "kiwoom" else settings.env,
            "feed": type(feed).__name__ if feed else None, "tick": state.tick, "themes": len(state.themes), "stocks": len(state.stocks),
            "clients": len(clients), "collect": last_collect | {"every_minutes": settings.collect_minutes},
            "overnight": {"asof": overnight_data.get("asof"), "base_at": overnight_data.get("base_at"), "every_minutes": settings.overnight_minutes},
            "schedule": {j.name: {"at": j.at or None, "lastDate": j.last_date, "ok": j.last_result.get("ok")} for j in scheduler.jobs}}


@app.get("/api/themes")
def themes():
    state.recompute()
    return state.snapshot()


@app.get("/api/stocks/{code}")
def stock(code: str):
    s = state.stocks.get(code)
    if not s:
        raise HTTPException(404, "모르는 종목코드")
    return s.as_dict() | {"theme": state.theme_of[code].id}


def _read_json(name: str) -> dict:
    p = settings.data_dir / name
    if not p.exists():
        return {}
    return json.loads(p.read_text("utf-8"))


@app.get("/api/screener/hoga")
def hoga():
    d = _read_json("hoga.json")
    if not d:
        raise HTTPException(404, "data/hoga.json 이 없습니다. scripts/fetch_daily.py 를 먼저 실행하세요.")
    return d


screener_job: dict = {"running": False, "done": 0, "total": 0, "current": "", "startedAt": None, "finishedAt": None, "error": None, "source": None, "universe": None}


async def _screener_task(source: str, universe: str, min_amount: float) -> None:
    def progress(i, n, name):
        screener_job.update(done=i, total=n, current=name)

    try:
        r = await run_screener(source, universe, min_amount_eok=min_amount, progress=progress)
        screener_job.update(error=None, rows=r["totalRecent"])
        log.info("스크리너 완료: %s/%s %d종목 → 최근 %d자리", r["source"], r["universe"], r["stocks"], r["totalRecent"])
    except Exception as e:
        screener_job.update(error=str(e))
        log.exception("스크리너 실패")
    finally:
        screener_job.update(running=False, finishedAt=datetime.now().isoformat(timespec="seconds"))


@app.post("/api/screener/run")
async def api_screener_run(source: str = "naver", universe: str = "themes", min_amount: float = 30.0):
    """일봉을 받아 고가놀이를 다시 찾는다. 백그라운드로 돌고 /api/screener/status 로 진행을 본다."""
    if screener_job["running"]:
        raise HTTPException(409, "이미 찾는 중입니다.")
    if source not in ("auto", "naver", "kis", "kiwoom", "mock") or universe not in ("themes", "market"):
        raise HTTPException(400, "source 는 auto|naver|kis|kiwoom|mock, universe 는 themes|market")
    screener_job.update(running=True, done=0, total=0, current="종목 목록 준비", startedAt=datetime.now().isoformat(timespec="seconds"),
                        finishedAt=None, error=None, source=source, universe=universe)
    asyncio.create_task(_screener_task(source, universe, min_amount), name="screener")
    return {"ok": True, "started": True}


@app.get("/api/screener/status")
def api_screener_status():
    return screener_job


# ── 스케줄 ─────────────────────────────────────────────────────
@app.get("/api/schedule")
def api_schedule():
    return scheduler.status()


@app.post("/api/schedule/run/{name}")
async def api_schedule_run(name: str):
    """스케줄 작업을 지금 바로 실행 (백그라운드). 결과는 /api/schedule 의 lastResult 에."""
    job = next((j for j in scheduler.jobs if j.name == name), None)
    if not job:
        raise HTTPException(404, f"모르는 작업: {name} (screener | collect)")
    if job.running:
        raise HTTPException(409, "이미 실행 중입니다.")
    asyncio.create_task(scheduler.run_job(job, force=True), name=f"schedule-{name}")
    return {"ok": True, "started": name}


@app.get("/api/overnight")
def overnight():
    return overnight_data or _read_json("overnight.json") or {"asof": None, "base_at": None, "rows": [], "notes": []}


@app.post("/api/overnight/refresh")
async def api_overnight_refresh():
    if overnight_lock.locked():
        raise HTTPException(409, "이미 수집 중입니다.")
    try:
        return await refresh_overnight()
    except Exception as e:
        raise HTTPException(502, f"야간 지표 수집 실패: {e}")


# ── 테마 갱신 ──────────────────────────────────────────────────
@app.post("/api/collect")
async def api_collect():
    return await run_collect()


@app.post("/api/news/refresh")
async def api_news_refresh():
    return await refresh_news()


@app.post("/api/themes/reload")
async def api_reload():
    await reload_state()
    return {"ok": True, "themes": len(state.themes), "stocks": len(state.stocks)}


# ── 스트림 ─────────────────────────────────────────────────────
@app.websocket("/ws/stream")
async def stream(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    try:
        await ws.send_text(json.dumps(state.snapshot() | {"type": "snapshot"}, ensure_ascii=False))
        while True:
            await ws.receive_text()  # 클라이언트 ping 등, 내용은 무시
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


app.mount("/", StaticFiles(directory=str(settings.static_dir), html=True), name="static")
