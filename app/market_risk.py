"""19:50 시점의 '내일 시장 갭다운 위험' 표시.

종가배팅(저녁 NXT 매수 → 다음 날 시가 매도)은 결국 다음 날 시장이 안 빠지는 데 거는 규칙이다.
과거 표본(2026-03~09, 120거래일)에서 다음 날 시장이 -1% 넘게 갭다운한 날의 종가배팅 승률은 27% 였다.

19:50 에 알 수 있는 정보로 그걸 얼마나 미리 알 수 있는지 검증했다 (scripts/sweep_mkt_risk.py).
15:30(KRX 마감)→19:45 사이 해외 지표 움직임과 다음 날 전 종목 시가갭 중앙값의 상관:
  나스닥 선물(NQ=F) +0.32 (15분봉 +0.40) · S&P 선물(ES=F) +0.28 · 니케이 선물 +0.26 · 환율 -0.13 · 유가 -0.11 · 항셍 +0.13
규칙별 (표시일 / 실제 -1% 갭다운 적중 / 급등주 -2%이하 눌림 종가배팅 일평균 수익, 표시 vs 미표시):
  NQ ≤ -0.3%   18일 / 9일 (평소 21/120=18%)  / +0.9% vs +7.4%  ← 채택 '주의'
  NQ ≤ -0.5%    8일 / 5일                     / +1.9% vs +6.6%
  ES ≤ -0.3%    7일 / 6일                     / +2.0% vs +6.7%  ← 채택 '위험' (표본 적음)
  환율 ≥ +0.3%  9일 / 3일  예측력 없음 → 참고 표시만.  유가 ≥ +2%  13일 / 3일  예측력 없음.
표시가 붙어도 승률 자체는 비슷하고(64% vs 66%) 평균 수익이 크게 줄어든다. 즉 '하지 마라' 가 아니라 '기대 수익이 작으니 수량을 줄이거나 쉬어라'.
2026-09-10 저녁은 NQ -0.35% 로 '주의' 가 붙었을 날이고, 다음 날 코스피200 은 -2.8% 갭다운했다.

시장이 열려 있는 동안(정규장 뒤 19:50 전)에는 15:30 대신 그날 15:30 을 기준으로 잡는다. 주말·휴일 저녁이면 마지막 거래일 15:30 이 기준이다.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import httpx

from app.collectors.overnight import DELAY, HEADERS, KST, YAHOO, series, value_at

log = logging.getLogger(__name__)

SYMBOLS = [("nq", "나스닥선물", "NQ=F"), ("es", "S&P선물", "ES=F"), ("cl", "WTI", "CL=F"), ("krw", "환율", "KRW=X")]
CAUTION_NQ = -0.3   # 주의
DANGER_NQ = -0.5    # 위험
DANGER_ES = -0.3


def base_time(now: datetime) -> datetime:
    """기준 시각 = 가장 최근 거래일(월~금)의 15:30 KST."""
    now = now.astimezone(KST)
    base = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if base > now:
        base -= timedelta(days=1)
    while base.weekday() >= 5:
        base -= timedelta(days=1)
    return base


def assess(moves: dict[str, float | None]) -> tuple[str, str]:
    """(등급, 설명). 등급은 위험 | 주의 | 보통 | 미상."""
    nq, es = moves.get("nq"), moves.get("es")
    if nq is None and es is None:
        return "미상", "해외 지표를 못 읽었습니다"
    if (nq is not None and nq <= DANGER_NQ) or (es is not None and es <= DANGER_ES):
        return "위험", "과거 이 정도면 다음 날 시장 갭다운이 8일 중 5일 · 종가배팅 일평균 +1.9% (평소 +6.6%). 쉬거나 수량 최소로"
    if nq is not None and nq <= CAUTION_NQ:
        return "주의", "과거 이 정도면 다음 날 시장 갭다운이 18일 중 9일 (평소 18%) · 종가배팅 일평균 +0.9% (평소 +7.4%). 수량을 줄이세요"
    return "보통", "해외 지표가 15:30 이후 크게 나쁘지 않음 (갭다운 확률 평소 수준 18%)"


def line(risk: dict) -> str:
    """알림에 넣는 한 줄."""
    parts = []
    for key, name, _ in SYMBOLS:
        m = risk["moves"].get(key)
        parts.append(f"{name} {m:+.2f}%" if m is not None else f"{name} -")
    return f"시장 위험 [{risk['level']}] 15:30 이후 {' · '.join(parts)} — {risk['note']}"


async def check(now: datetime | None = None, client: httpx.AsyncClient | None = None) -> dict:
    now = (now or datetime.now(KST)).astimezone(KST)
    base = base_time(now)
    moves: dict[str, float | None] = {}
    at: dict[str, str] = {}
    own = client is None
    client = client or httpx.AsyncClient(timeout=15, headers=HEADERS)
    try:
        for key, _, sym in SYMBOLS:
            try:
                r = await client.get(YAHOO.format(symbol=sym), params={"range": "5d", "interval": "15m", "includePrePost": "true"})
                r.raise_for_status()
                pts = series(r.json())
                a, b = value_at(pts, base), value_at(pts, now)
                if a and b and b[1] > a[1] and a[1] >= base.timestamp() - 3 * 3600:
                    moves[key] = round((b[0] / a[0] - 1) * 100, 2)
                    at[key] = datetime.fromtimestamp(b[1], KST).strftime("%H:%M")
                else:
                    moves[key] = None
            except Exception as e:
                log.warning("시장 위험 지표 %s 실패: %s", sym, e)
                moves[key] = None
            await asyncio.sleep(DELAY)
    finally:
        if own:
            await client.aclose()
    level, note = assess(moves)
    return {"at": now.isoformat(timespec="seconds"), "base": base.isoformat(timespec="minutes"), "moves": moves, "movesAt": at,
            "level": level, "note": note}
