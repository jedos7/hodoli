"""리서치 브리핑 — 네이버 증권 리서치 (JSON API).

  목록: https://m.stock.naver.com/api/research/{industry|economy|market|invest|company}?page=N&pageSize=M
        [{researchId, title, brokerName, writeDate(YYYY-MM-DD), category, itemCode·itemName(종목분석만), endUrl}]
  상세: https://m.stock.naver.com/api/research/{kind}/{researchId}  → researchContent.content (HTML 요약 원문, 종목분석은 목표주가·투자의견 문장 포함)

2026-09-11 까지 쓰던 finance.naver.com/research 의 HTML 게시판은 그날부터 stock.naver.com 으로 302 넘어갔다.
종목별 리포트 검색(searchType=itemCode)은 새 API 에 없어서, 종목분석 목록을 통째로(최근 수백 건) 받아 종목코드로 거른다.

브리핑 = 최근 N일 산업·경제·시황·투자정보 리포트의 요약 문장(원문 그대로) + 테마별 목표가 추정치 방향
  추정치 방향: 테마 종목의 최근 N일 종목 리포트마다, 같은 증권사의 직전 리포트 목표가와 비교해 상향/하향을 센다.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from app.collectors.naver_api import NaverApi

log = logging.getLogger(__name__)

LISTS = [("industry", "산업"), ("economy", "경제"), ("market", "시황"), ("invest", "투자정보")]
COMPANY_PAGES = 3          # 종목분석 목록을 500건씩 몇 쪽 (3쪽 ≈ 최근 6~8주)
COMPANY_PAGE_SIZE = 500


@dataclass(slots=True)
class Report:
    kind: str        # 산업 | 경제 | 시황 | 투자정보 | 종목
    category: str    # 산업 분류 또는 종목명
    title: str
    broker: str
    date: str        # YYYY-MM-DD
    nid: int
    url: str
    bullets: list[str] = field(default_factory=list)
    target: int | None = None
    opinion: str = ""
    code: str = ""   # 종목분석이면 종목코드


def parse_list(rows: list[dict], kind: str) -> list[Report]:
    out = []
    for r in rows or []:
        try:
            nid = int(r["researchId"])
        except (KeyError, TypeError, ValueError):
            continue
        cat = r.get("itemName") if kind == "종목" else r.get("category")
        out.append(Report(kind, html.unescape(str(cat or "")).strip(), html.unescape(str(r.get("title", ""))).strip(),
                          html.unescape(str(r.get("brokerName", ""))).strip(), str(r.get("writeDate", ""))[:10], nid,
                          str(r.get("endUrl", "")), code=str(r.get("itemCode") or "")))
    return out


_TARGET = re.compile(r"목표(?:주가|가)\s*(?:은|는|를|을)?\s*([\d,]{4,})\s*원")
_OPINION = re.compile(r"투자의견\s*(?:은|는|을|를)?\s*[\"'“”]?\s*(매수|중립|보유|매도|비중확대|비중축소|시장수익률|BUY|HOLD|SELL|Buy|Hold|Sell|Outperform|Neutral|Underperform)")


def parse_detail(d: dict, max_bullets: int = 4) -> dict:
    """researchContent.content(HTML) 를 문장 단위로 잘라 bullets 로. 목표가·투자의견도 뽑는다."""
    body = ((d or {}).get("researchContent") or {}).get("content") or ""
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    body = re.sub(r"</?(p|br|div|li|h\d)[^>]*>", "\n", body)
    text = html.unescape(re.sub(r"<[^>]+>", "", body))
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if len(ln) >= 8]
    bullets: list[str] = []
    if lines:
        head = lines[0]
        rest = " ".join(lines[1:]) if len(lines) > 1 else ""
        if not rest and len(head) > 120:      # 새 API 는 요약이 한 문단으로 오는 일이 많다 → 첫 줄도 문장으로 자른다
            parts = [s.strip() for s in re.split(r"(?<=[.다요음됨함])\s+", head) if s.strip()]
            head, rest = parts[0], " ".join(parts[1:])
        sents = [s.strip() for s in re.split(r"(?<=[.다요음됨함])\s+", rest) if len(s.strip()) >= 10]
        bullets = [head] + sents[: max_bullets - 1]
    flat = re.sub(r"\s+", " ", text)
    t = _TARGET.search(flat)
    o = _OPINION.search(flat)
    return {"bullets": bullets, "target": int(t.group(1).replace(",", "")) if t else None, "opinion": o.group(1) if o else ""}


def _api_path(r: Report) -> str:
    """endUrl https://m.stock.naver.com/research/company/96103 → /research/company/96103"""
    if "/research/" in r.url:
        return "/research/" + r.url.split("/research/", 1)[1].split("?")[0]
    return f"/research/company/{r.nid}"


class NaverResearch(NaverApi):
    def __init__(self, client=None):
        super().__init__(client)
        self._company: list[Report] | None = None

    async def list_reports(self, list_key: str, kind: str, since: str, pages: int = 2, page_size: int = 100) -> list[Report]:
        out: list[Report] = []
        for p in range(1, pages + 1):
            rows = parse_list(await self.json(f"/research/{list_key}", {"page": p, "pageSize": page_size}), kind)
            out += [r for r in rows if r.date >= since]
            if not rows or rows[-1].date < since:
                break
        return out

    async def fill_detail(self, r: Report) -> Report:
        try:
            d = parse_detail(await self.json(_api_path(r)))
            r.bullets, r.target, r.opinion = d["bullets"], d["target"], d["opinion"]
        except Exception as e:
            log.debug("리포트 상세 실패 %s: %s", r.nid, e)
        return r

    async def company_all(self) -> list[Report]:
        """종목분석 목록 최근 수백 건 (한 번만 받아 둔다)."""
        if self._company is None:
            out: list[Report] = []
            for p in range(1, COMPANY_PAGES + 1):
                rows = parse_list(await self.json("/research/company", {"page": p, "pageSize": COMPANY_PAGE_SIZE}), "종목")
                out += rows
                if len(rows) < COMPANY_PAGE_SIZE:
                    break
            self._company = out
        return self._company

    async def company_reports(self, code: str) -> list[Report]:
        return [r for r in await self.company_all() if r.code == code]

    async def target_direction(self, stocks: list[tuple[str, str]], since: str, max_reports: int = 8) -> dict:
        """테마 종목들의 최근 리포트 목표가를 같은 증권사 직전 리포트와 비교 → {"up": n, "down": m, "items": [...]}"""
        up = down = 0
        items = []
        for code, name in stocks:
            try:
                reps = await self.company_reports(code)
            except Exception as e:
                log.debug("종목 리포트 목록 실패 %s: %s", code, e)
                continue
            recent = [r for r in reps if r.date >= since][:max_reports]
            for r in recent:
                prev = next((p for p in reps if p.broker == r.broker and p.date < r.date), None)
                if not prev:
                    continue
                await self.fill_detail(r)
                await self.fill_detail(prev)
                if r.target and prev.target and r.target != prev.target:
                    if r.target > prev.target:
                        up += 1
                    else:
                        down += 1
                    items.append({"stock": name, "broker": r.broker, "date": r.date, "from": prev.target, "to": r.target, "opinion": r.opinion, "url": r.url})
        return {"up": up, "down": down, "items": items}


async def briefing(themes: list[tuple[str, list[tuple[str, str]]]], days: int = 7, max_reports: int = 40) -> dict:
    """themes = [(테마명, [(code, name), ...])]. 결과는 data/research.json 형식."""
    since = (date.today() - timedelta(days=days)).isoformat()
    nr = NaverResearch()
    try:
        reports: list[Report] = []
        for key, kind in LISTS:
            try:
                reports += await nr.list_reports(key, kind, since)
            except Exception as e:
                log.warning("리서치 목록 실패 %s: %s", key, e)
        reports.sort(key=lambda r: (r.date, r.nid), reverse=True)
        reports = reports[:max_reports]
        for r in reports:
            await nr.fill_detail(r)
        direction = []
        for name, stocks in themes:
            d = await nr.target_direction(stocks, since)
            if d["up"] or d["down"]:
                direction.append({"theme": name, **d})
        direction.sort(key=lambda x: (x["up"] - x["down"], x["up"]), reverse=True)
        return {"asof": datetime.now().isoformat(timespec="seconds"), "days": days, "since": since,
                "direction": direction, "reports": [asdict(r) for r in reports]}
    finally:
        await nr.close()
