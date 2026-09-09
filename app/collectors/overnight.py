"""야간 지표 수집기 — "전일 20:05 (NXT 마감) 대비".

국내 야간 거래(NXT)가 끝나는 20:05 를 기준점으로 잡고, 그 뒤 미국 프리장·유가·환율이 얼마나 움직였는지 보여준다.
기준값은 따로 저장하지 않고, 야후 파이낸스 차트 API 의 15분봉 이력에서 '기준 시각 이전 마지막 봉'을 뽑는다.
그래서 서버가 20:05 에 떠 있지 않아도 되고, 재시작해도 값이 같다.

  https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=15m&includePrePost=true

장이 닫힌 지수(코스피200·대만 가권 등)는 기준값 = 지금 = 마지막 종가라 0.00% 로 나온다. 의도된 동작이다.
'외인 선물' 은 야후에 없다. KIS 선물 투자자 매매동향 API 를 붙일 자리만 두고 값은 비워 둔다.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9), "KST")  # 한국은 서머타임이 없어 고정 오프셋으로 충분 (Windows 에 tz DB 가 없어도 동작)
BASE_TIME = time(20, 5)  # NXT 마감
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "application/json",
}
DELAY = 0.15

# (키, 표시 이름, 야후 심볼 후보들, 소수 자리, 해석 태그)
SYMBOLS: list[tuple[str, str, list[str], int, str | None]] = [
    ("nasdaq", "나스닥", ["^IXIC"], 2, None),
    ("ks200", "코스피200", ["^KS200"], 2, None),
    ("wti", "WTI", ["CL=F"], 2, "oil"),
    ("brent", "브렌트", ["BZ=F"], 2, None),
    ("usdkrw", "환율", ["KRW=X"], 2, "fx"),
    ("skhy_gdr", "SKHY GDR(€)", ["HY9H.SG", "HY9H.F"], 2, None),  # 미국 ADR 은 야후에 없어 슈투트가르트 GDR(유로) 사용
    ("vix", "VIX", ["^VIX"], 2, "vix"),
    ("dxy", "달러지수", ["DX-Y.NYB"], 2, None),
    ("gold", "금", ["GC=F"], 2, None),
    ("sox", "필라델피아 반도체", ["^SOX"], 2, None),
    ("smh", "SMH", ["SMH"], 2, None),
    ("mu", "마이크론", ["MU"], 2, None),
    ("tsm", "TSMC", ["TSM"], 2, None),
    ("kioxia", "키옥시아", ["285A.T"], 0, None),
    ("sndk", "샌디스크", ["SNDK"], 2, None),
    ("twii", "대만 가권", ["^TWII"], 2, None),
    ("frgn_fut", "외인 선물", [], 0, None),  # KIS 연동 자리
]


@dataclass(slots=True)
class Row:
    key: str
    name: str
    symbol: str | None
    prev: float | None      # 기준(전일 20:05) 값
    now: float | None
    chg: float | None       # %
    digits: int
    tag: str | None
    prev_at: str | None     # 기준값이 실제로 찍힌 봉 시각 (KST ISO)
    now_at: str | None
    error: str | None = None
    diff: float | None = None   # % 가 의미 없는 지표(계약 수 등)는 차이값으로
    src: str | None = None


def baseline_time(now: datetime | None = None) -> datetime:
    """가장 최근에 지나간 20:05 KST."""
    now = (now or datetime.now(KST)).astimezone(KST)
    base = now.replace(hour=BASE_TIME.hour, minute=BASE_TIME.minute, second=0, microsecond=0)
    if base > now:
        base -= timedelta(days=1)
    return base


def series(chart: dict) -> list[tuple[int, float]]:
    """(epoch, close) 목록. 빈 봉은 뺀다."""
    r = chart["chart"]["result"][0]
    ts = r.get("timestamp") or []
    closes = r["indicators"]["quote"][0].get("close") or []
    return [(t, c) for t, c in zip(ts, closes) if c is not None]


def value_at(points: list[tuple[int, float]], at: datetime) -> tuple[float, int] | None:
    """at 이전(같은 시각 포함) 마지막 값."""
    epoch = int(at.timestamp())
    prev = None
    for t, c in points:
        if t <= epoch:
            prev = (c, t)
        else:
            break
    return prev


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, KST).isoformat(timespec="minutes")


async def fetch_chart(client: httpx.AsyncClient, symbol: str) -> dict | None:
    r = await client.get(YAHOO.format(symbol=symbol), params={"range": "5d", "interval": "15m", "includePrePost": "true"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    d = r.json()
    if not d.get("chart", {}).get("result"):
        return None
    return d


async def collect(now: datetime | None = None, client: httpx.AsyncClient | None = None,
                  extra: dict[str, dict] | None = None) -> dict:
    """extra: 야후에 없는 지표를 호출자가 채운다. {key: {"prev":…, "now":…, "src":…, "error":…}} (외인 선물 등)"""
    own = client is None
    c = client or httpx.AsyncClient(headers=HEADERS, timeout=15)
    base = baseline_time(now)
    rows: list[Row] = []
    extra = extra or {}
    try:
        for key, name, symbols, digits, tag in SYMBOLS:
            row = Row(key, name, None, None, None, None, digits, tag, None, None)
            if not symbols:
                x = extra.get(key)
                if x is None:
                    row.error = "소스 없음 (KIS 선물 투자자 API 연결 필요)"
                else:
                    row.prev, row.now, row.src, row.error = x.get("prev"), x.get("now"), x.get("src"), x.get("error")
                    if row.now is None and row.prev is not None:
                        row.now = row.prev  # 장중 값이 없으면 전일 값 유지 (변화 0)
                    if row.prev is not None and row.now is not None:
                        row.diff = row.now - row.prev
                rows.append(row)
                continue
            for sym in symbols:
                try:
                    chart = await fetch_chart(c, sym)
                except httpx.HTTPError as e:
                    row.error = f"{sym}: {e}"
                    continue
                finally:
                    await asyncio.sleep(DELAY)
                if not chart:
                    row.error = f"{sym}: 심볼 없음"
                    continue
                pts = series(chart)
                got = value_at(pts, base)
                if not pts or not got:
                    row.error = f"{sym}: 기준 시각 이전 봉 없음"
                    continue
                prev, prev_t = got
                now_v, now_t = pts[-1][1], pts[-1][0]
                row.symbol, row.prev, row.now = sym, prev, now_v
                row.chg = (now_v / prev - 1) * 100 if prev else None
                row.prev_at, row.now_at, row.error = _iso(prev_t), _iso(now_t), None
                break
            rows.append(row)
    finally:
        if own:
            await c.aclose()
    return {
        "asof": datetime.now(KST).isoformat(timespec="seconds"),
        "base_at": base.isoformat(timespec="minutes"),
        "rows": [asdict(r) for r in rows],
        "notes": notes(rows),
    }


def notes(rows: list[Row]) -> list[str]:
    """유가·환율·VIX 한 줄 해석. 프론트도 같은 규칙을 쓴다."""
    by = {r.tag: r for r in rows if r.tag and r.chg is not None}
    out = []
    if (r := by.get("oil")):
        out.append(f"유가(WTI) {r.chg:+.2f}% — " + ("큰 변화 없음" if abs(r.chg) < 1 else "유가 상승, 정유·조선 체크" if r.chg > 0 else "유가 하락, 항공·화학 우호"))
    if (r := by.get("fx")):
        out.append(f"환율 {r.chg:+.2f}% — " + ("원화 강세, 외인 수급 우호" if r.chg < -0.3 else "원화 약세, 외인 이탈 주의" if r.chg > 0.3 else "중립"))
    if (r := by.get("vix")):
        out.append(f"VIX {r.chg:+.2f}% — " + ("공포 완화" if r.chg < -5 else "변동성 확대" if r.chg > 5 else "중립"))
    return out
