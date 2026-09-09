"""NXT 하락 마감 종가배팅 알림.

검증(scripts/sweep_close_nxt.py) 결과: 종가배팅은 15:30 KRX 종가보다 20:00 NXT 종가가 낫고,
그중에서도 NXT 에서 KRX 종가보다 '내려' 마감한 날이 좋았다 (급등주 +8%↑ 갭 승률 53.6%, 평균 +1.5% / 고가놀이 자리 70.6%, +2.1%).

그래서 매일 19:50(설정 SCHEDULE_NXT)에 후보 종목의 NXT 현재가를 키움에서 읽어 KRX 종가와 비교하고,
내려 있는 종목을 "종가배팅 후보" 알림으로 낸다. 후보:
  1) 오늘 고가놀이 엄선 자리 (data/hoga.json 의 asof 날짜 행)
  2) 오늘 +8% 이상 급등, 거래대금 50억 이상, 상한가(+29%) 제외 (일봉 캐시 data/candles_kiwoom_market.json)
주문은 하지 않는다. 결과는 data/nxt_close.json 에 남고 /api/nxtclose 로 본다.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime

from app.config import settings

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    code: str
    name: str
    why: str            # 고가놀이 | 급등 +x%
    krx_close: int
    krx_chg: float
    amount_eok: float
    nx_price: int = 0
    nx_move: float = 0.0   # NXT 현재가 / KRX 종가 - 1 (%)
    pick: bool = False     # NXT 하락 → 후보
    nx_traded: bool = False  # NXT 에서 거래(가격)가 있었는가
    near_high: float | None = None  # 낮 종가가 당일 고가 대비 몇 % (0 = 고가 마감, 음수 = 밀려 마감)


def candles_today() -> bool:
    """일봉 캐시의 마지막 날짜가 오늘인가. 아니면(휴장·스크리너 미실행) 종가배팅 비교가 무의미하다."""
    c = settings.data_dir / "candles_kiwoom_market.json"
    if not c.exists():
        return False
    try:
        d = json.loads(c.read_text("utf-8"))
        last = max((rows[-1][0] for rows in d["candles"].values() if rows), default="")
        return last == datetime.now().strftime("%Y%m%d")
    except (OSError, ValueError, KeyError, IndexError):
        return False


def load_candidates(min_spike: float = 8.0, min_amount_eok: float = 50.0) -> list[Candidate]:
    out: dict[str, Candidate] = {}
    # 1) 고가놀이 엄선 (오늘)
    p = settings.data_dir / "hoga.json"
    if p.exists():
        d = json.loads(p.read_text("utf-8"))
        for r in d.get("rows", []):
            if r.get("strict") and r["date"] == str(d.get("asof")):
                out[r["code"]] = Candidate(r["code"], r["name"], "고가놀이", int(r["close"]), 0.0, float(r["amountEok"]))
    # 2) 오늘 급등주
    c = settings.data_dir / "candles_kiwoom_market.json"
    if c.exists():
        d = json.loads(c.read_text("utf-8"))
        names = {u[0]: u[1] for u in d.get("universeList", [])}
        for code, rows in d["candles"].items():
            if len(rows) < 2:
                continue
            last, prev = rows[-1], rows[-2]
            if prev[4] <= 0:
                continue
            chg = (last[4] / prev[4] - 1) * 100
            amt = last[6] / 1e8
            near = (last[4] / last[2] - 1) * 100 if last[2] else None   # 종가 vs 고가
            if chg >= min_spike and chg < 29 and amt >= min_amount_eok:
                if code in out:
                    out[code].krx_chg, out[code].near_high = chg, near
                else:
                    out[code] = Candidate(code, names.get(code, code), f"급등 {chg:+.1f}%", int(last[4]), chg, amt, near_high=near)
            elif code in out:
                out[code].krx_chg, out[code].near_high = chg, near
    return list(out.values())


async def check(rest, candidates: list[Candidate], closing: bool) -> dict:
    """키움 ka10001 을 NXT 코드(코드_NX)로 조회해 NXT 현재가를 얻는다."""
    for cnd in candidates:
        try:
            b = await rest.basic(cnd.code + "_NX")
            if b.price > 0 and cnd.krx_close > 0:
                cnd.nx_price = b.price
                cnd.nx_traded = True
                cnd.nx_move = (b.price / cnd.krx_close - 1) * 100
                cnd.pick = cnd.nx_move < 0
        except Exception as e:
            log.debug("NXT 조회 실패 %s: %s", cnd.code, e)
    picks = sorted((c for c in candidates if c.pick), key=lambda c: c.nx_move)
    result = {"at": datetime.now().isoformat(timespec="seconds"), "closing": closing, "candidates": [asdict(c) for c in candidates],
              "picks": [asdict(c) for c in picks]}
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "nxt_close.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), "utf-8")
    return result


def score(c: dict) -> tuple[float, list[str]]:
    """후보 점수와 근거. 2026-09 NXT 하락 마감 급등주 922건 검증:
    NXT 눌림 -3% 이하 승률 77%/+7.9% · 거래대금 1,000억↑ 63%/+2.7% · 낮에 고가 대비 3% 넘게 밀려 마감 61%/+3.0% · 고가놀이 자리 71%/+2.1%"""
    s, why = 0.0, []
    m = c.get("nx_move", 0.0)
    if m <= -3:
        s += 3; why.append(f"NXT 눌림 {m:.1f}% (검증 최상)")
    elif m <= -1:
        s += 1; why.append(f"NXT 눌림 {m:.1f}%")
    else:
        why.append(f"NXT 눌림 {m:.1f}% (얕음)")
    amt = c.get("amount_eok", 0.0)
    if amt >= 1000:
        s += 2; why.append(f"대금 {amt:,.0f}억 (큰 종목)")
    elif amt >= 200:
        s += 1; why.append(f"대금 {amt:,.0f}억")
    else:
        s -= 1; why.append(f"대금 {amt:,.0f}억 (작음)")
    nh = c.get("near_high")
    if nh is not None:
        if nh <= -3:
            s += 1.5; why.append("낮에 고가에서 밀려 마감")
        elif nh >= -1:
            s -= 0.5; why.append("낮에 고가 마감")
    if c.get("why") == "고가놀이":
        s += 2; why.append("고가놀이 자리")
    return s, why


def alerts_for(result: dict, top: int = 3) -> list[dict]:
    picks = result["picks"]
    when = "NXT 마감" if result["closing"] else "NXT 현재"
    at = result["at"][11:19]
    if not picks:
        n = len(result["candidates"])
        return [{"kind": "nxt", "code": "", "name": "", "at": at,
                 "text": f"종가배팅 후보 없음 — {when} 기준 급등주 {n}종목 중 저녁에 눌린 종목이 없습니다"}]
    ranked = sorted(((score(c), c) for c in picks), key=lambda x: x[0][0], reverse=True)
    lines = [f"🌙 종가배팅 후보 (저녁에 눌린 급등주) · {when} 기준 · 눌린 {len(picks)}종목 중 상위 {min(top, len(ranked))}"]
    out = []
    for i, ((s, why), c) in enumerate(ranked[:top], 1):
        lines.append(f"{i}. {c['name']} ({c['why']}) {c['krx_close']:,}→{c['nx_price']:,}원 ({c['nx_move']:+.1f}%) · {' · '.join(why)}")
    if len(ranked) > top:
        lines.append(f"외 {len(ranked) - top}종목은 화면 목록(/api/nxtclose)에서")
    lines.append("규칙: NXT 종가 근처에 지정가 매수 → 다음 날 시가 매도. 갭 하락 위험이 있으니 수량은 작게.")
    out.append({"kind": "nxt", "code": ranked[0][1]["code"], "name": ranked[0][1]["name"], "at": at, "text": "\n".join(lines),
                "top": [{"rank": i, "code": c["code"], "name": c["name"], "score": s, "why": why} for i, ((s, why), c) in enumerate(ranked[:top], 1)]})
    return out
