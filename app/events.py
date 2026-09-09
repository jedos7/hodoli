"""일정 — 앞으로 N일의 시장 이벤트. 규칙으로 계산되는 것 + 고정 목록(FOMC) + 손으로 등록한 것(data/events.json).

규칙 (한국 시각 기준, '추정' 표시가 붙은 것은 공식 일정을 확인해야 한다)
  옵션 만기       매월 둘째 목요일. 3·6·9·12월은 선물·옵션 동시만기(네 마녀) — 장중 프로그램 매매·마감 변동 주의
  미국 고용보고서  매월 첫째 금요일 22:30 (추정)
  ISM 제조업      매월 첫 영업일 (추정)
  미국 CPI        매월 12일 근처 평일 (추정), PPI 는 그 다음 평일 (추정)
  미국 PCE        매월 마지막 금요일 (추정)
  FOMC            2026년 공식 회의 둘째 날 (결과 발표, 한국 시각 다음 날 새벽)
학회·컨퍼런스·국내 실적 발표·신규상장은 자동 수집이 없어 등록으로 넣는다. 등록 항목에 themeId 를 주면 테마 카드에 D-n 이 뜬다.
"""
from __future__ import annotations

import json
import uuid
from calendar import monthrange
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

FOMC_2026 = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"]
WEEKDAY_KO = "월화수목금토일"


@dataclass
class Event:
    date: str          # YYYY-MM-DD
    kind: str          # 지표 | 정책 | 실적 | 상장 | 학회 | 기타
    title: str
    note: str = ""
    importance: int = 2  # 1~3 (● 수)
    estimated: bool = False
    source: str = "자동"  # 자동 | 등록
    id: str = ""
    themeId: str = ""

    def as_dict(self, today: date) -> dict:
        d = asdict(self)
        dt = date.fromisoformat(self.date)
        d["dday"] = (dt - today).days
        d["weekday"] = WEEKDAY_KO[dt.weekday()]
        return d


def _nth_weekday(y: int, m: int, weekday: int, n: int) -> date:
    first = date(y, m, 1)
    off = (weekday - first.weekday()) % 7
    return first + timedelta(days=off + 7 * (n - 1))


def _last_weekday(y: int, m: int, weekday: int) -> date:
    last = date(y, m, monthrange(y, m)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _next_bday(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def rule_events(start: date, end: date) -> list[Event]:
    out: list[Event] = []
    y, m = start.year, start.month
    seen_months = []
    while date(y, m, 1) <= end:
        seen_months.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    # 이전 달도 포함해야 '이번 달 초' 이벤트가 잡힌다
    py, pm = (start.year, start.month - 1) if start.month > 1 else (start.year - 1, 12)
    for y, m in [(py, pm)] + seen_months:
        thu2 = _nth_weekday(y, m, 3, 2)
        if m in (3, 6, 9, 12):
            out.append(Event(thu2.isoformat(), "지표", "선물·옵션 동시만기 (네 마녀)", "지수선물·옵션·주식선물·주식옵션 동시만기 — 장중 프로그램 매매·마감 변동 주의", 3))
        else:
            out.append(Event(thu2.isoformat(), "지표", "코스피200 옵션 만기", "매월 둘째 목요일 — 프로그램 매매가 커진다", 2))
        out.append(Event(_nth_weekday(y, m, 4, 1).isoformat(), "지표", "미국 고용보고서(비농업)", "매월 첫째 금요일 22:30(한국) — 날짜는 추정, 공식 일정 확인 필요", 3, True))
        out.append(Event(_next_bday(date(y, m, 1)).isoformat(), "지표", "ISM 제조업지수", "매월 첫 영업일 — 추정", 2, True))
        cpi = _next_bday(date(y, m, 12))
        out.append(Event(cpi.isoformat(), "지표", "미국 CPI", "날짜는 추정 — 공식 일정 확인 필요", 3, True))
        out.append(Event(_next_bday(cpi + timedelta(days=1)).isoformat(), "지표", "미국 PPI", "날짜는 추정 — 공식 일정 확인 필요", 2, True))
        out.append(Event(_last_weekday(y, m, 4).isoformat(), "지표", "미국 PCE 물가", "매월 마지막 금요일 — 추정, 공식 일정 확인 필요", 2, True))
    for d in FOMC_2026:
        out.append(Event(d, "정책", "FOMC 결과 발표", "회의 둘째 날 (한국 시각 다음 날 새벽 3시). 경제전망(SEP) 동반 시 변동 확대", 3))
    return [e for e in out if start <= date.fromisoformat(e.date) <= end]


class Events:
    def __init__(self, path: Path):
        self.path = path
        self.manual: list[Event] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self.manual = [Event(**{**x, "source": "등록"}) for x in json.loads(self.path.read_text("utf-8"))]
        except (OSError, ValueError, TypeError):
            self.manual = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(e) for e in self.manual], ensure_ascii=False, indent=1), "utf-8")

    def add(self, date_: str, kind: str, title: str, note: str = "", importance: int = 2, theme_id: str = "") -> Event:
        e = Event(date_, kind, title, note, max(1, min(3, importance)), False, "등록", uuid.uuid4().hex[:8], theme_id)
        self.manual.append(e)
        self._save()
        return e

    def remove(self, id_: str) -> bool:
        n = len(self.manual)
        self.manual = [e for e in self.manual if e.id != id_]
        if len(self.manual) != n:
            self._save()
            return True
        return False

    def upcoming(self, days: int = 30, today: date | None = None) -> dict:
        today = today or date.today()
        end = today + timedelta(days=days)
        auto = rule_events(today, end)
        manual = [e for e in self.manual if today <= date.fromisoformat(e.date) <= end]
        items = sorted(auto + manual, key=lambda e: (e.date, -e.importance))
        return {"today": today.isoformat(), "days": days, "auto": len(auto), "manual": len(manual),
                "items": [e.as_dict(today) for e in items]}
