"""오늘 장세 맥락 (app/regime.py). 표시만 하므로 매수 흐름은 건드리지 않는다."""
import asyncio
import json
from datetime import date, datetime, timedelta

import pytest

from app.autosell import KST
from app.regime import GATES, MAX_AGE_DAYS, RULES, Regime, cell, naver_index_change, yahoo_change

TODAY = date(2026, 10, 20)
NOW = datetime(2026, 10, 20, 8, 0, tzinfo=KST)


class Reply:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class Client:
    """야후·네이버 응답을 흉내 낸다. 호출한 주소를 기록한다."""

    def __init__(self, payloads):
        self.payloads, self.urls = payloads, []

    async def get(self, url, params=None, headers=None):
        self.urls.append(url)
        for key, payload in self.payloads.items():
            if key in url or key in json.dumps(params or {}):
                return Reply(payload)
        raise AssertionError(f'준비 안 된 주소: {url} {params}')

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def yahoo(bars):
    """bars = [(YYYY-MM-DD, 종가 또는 None)]"""
    stamps = [int(datetime.fromisoformat(d + 'T09:00+09:00').timestamp()) for d, _ in bars]
    return {'chart': {'result': [{'timestamp': stamps,
                                  'indicators': {'quote': [{'close': [c for _, c in bars]}]}}]}}


def naver(rows):
    return [{'localTradedAt': d, 'fluctuationsRatio': f'{r}'} for d, r in rows]


# ── 칸 가르기 ────────────────────────────────────────────────
def test_cell_picks_good_bad_and_middle():
    good, bad = (0.43, 'above', ''), (-0.14, 'below', '')
    assert cell(0.5, good, bad) == 'good'
    assert cell(0.43, good, bad) == 'mid'          # 문턱은 '위'가 아니다
    assert cell(0.0, good, bad) == 'mid'
    assert cell(-0.2, good, bad) == 'bad'
    assert cell(None, good, bad) == 'unknown'


def test_closebet_cells_follow_the_won():
    _, _, _, _, _, _, good, bad, _ = [r for r in RULES if r[0] == 'closebet'][0]
    assert cell(-0.56, good, bad) == 'good'        # 원화 강세
    assert cell(-0.1, good, bad) == 'mid'
    assert cell(0.3, good, bad) == 'bad'           # 원화 약세


# ── 값 읽기 ─────────────────────────────────────────────────
def test_yahoo_uses_the_last_bar_before_today():
    payload = yahoo([('2026-10-15', 100.0), ('2026-10-16', 101.0), ('2026-10-19', 102.0), ('2026-10-20', 999.0)])
    c = Client({'GSPC': payload})
    value, day = asyncio.run(yahoo_change(c, '^GSPC', TODAY))
    assert day == '2026-10-19' and value == pytest.approx((102 / 101 - 1) * 100)   # 오늘 봉은 안 쓴다


def test_stale_data_is_refused_instead_of_silently_used():
    """2026-10-20 확인: 야후 ^KS11 의 10-19 봉 종가가 비어 와서 10-16 값이 '전날'로 둔갑했다.
    오래된 봉은 값으로 쓰지 않고 오류를 올린다."""
    old = TODAY - timedelta(days=MAX_AGE_DAYS + 1)
    payload = yahoo([((old - timedelta(days=1)).isoformat(), 100.0), (old.isoformat(), 103.0),
                     ('2026-10-19', None)])                       # 최신 봉 종가가 비어 있다
    with pytest.raises(ValueError, match='오래됐습니다'):
        asyncio.run(yahoo_change(Client({'GSPC': payload}), '^GSPC', TODAY))


def test_naver_index_reads_the_previous_session():
    rows = naver([('2026-10-20', '0.50'), ('2026-10-19', '1.76'), ('2026-10-16', '2.05')])
    value, day = asyncio.run(naver_index_change(Client({'KOSPI': rows}), 'KOSPI', TODAY))
    assert day == '2026-10-19' and value == 1.76


# ── 전체 읽기 ───────────────────────────────────────────────
def payloads(kospi='1.76', spx=(101.0, 102.0), krw=(1340.0, 1333.0)):
    return {
        'KOSPI': naver([('2026-10-19', kospi), ('2026-10-16', '2.05')]),
        'GSPC': yahoo([('2026-10-16', spx[0]), ('2026-10-19', spx[1])]),
        'KRW=X': yahoo([('2026-10-16', krw[0]), ('2026-10-19', krw[1])]),
    }


def read(monkeypatch, client):
    import app.regime as mod
    monkeypatch.setattr(mod.httpx, 'AsyncClient', lambda **kw: client)
    return asyncio.run(Regime(lambda: NOW).read())


def test_reads_all_four_rows(monkeypatch):
    d = read(monkeypatch, Client(payloads()))
    rows = {r['key']: r for r in d['rows']}
    assert d['error'] == '' and len(rows) == 4
    assert rows['leaderdip']['value'] == 1.76 and rows['leaderdip']['cell'] == 'mid'
    assert rows['firebar']['cell'] == 'good' and rows['firebar']['value'] == pytest.approx(0.99, abs=0.01)
    assert rows['closebet']['cell'] == 'good'          # 원달러 1340 → 1333 (원화 강세)
    assert rows['horizonbuy']['cell'] == 'steady'


def test_one_source_failing_does_not_hide_the_others(monkeypatch):
    bad = payloads()
    bad['GSPC'] = {'chart': {'result': []}}
    d = read(monkeypatch, Client(bad))
    rows = {r['key']: r for r in d['rows']}
    assert rows['firebar']['cell'] == 'unknown' and rows['firebar']['error']
    assert rows['leaderdip']['cell'] == 'mid' and rows['closebet']['cell'] == 'good'
    assert d['error'] == ''


def test_result_is_remembered_for_the_day(monkeypatch):
    client = Client(payloads())
    import app.regime as mod
    monkeypatch.setattr(mod.httpx, 'AsyncClient', lambda **kw: client)
    r = Regime(lambda: NOW)
    asyncio.run(r.read())
    calls = len(client.urls)
    asyncio.run(r.read())
    assert len(client.urls) == calls                  # 30분 안에는 다시 받지 않는다
    asyncio.run(r.read(force=True))
    assert len(client.urls) > calls


# ── 🧭 맥락 문 (제작자 10-20 실제 규칙) ──────────────────────
def test_gate_rests_leaderdip_when_kospi_rose_yesterday(monkeypatch):
    """제작자 규칙 그대로: 전날 코스피가 0% 위면 🗻 는 그날 쉰다 (분석표의 +2.3% 가 아니다)."""
    r = Regime(lambda: NOW)
    import app.regime as mod
    monkeypatch.setattr(mod.httpx, 'AsyncClient', lambda **kw: Client(payloads(kospi='0.01')))
    assert asyncio.run(r.gate('leaderdip'))[0] is True
    r2 = Regime(lambda: NOW)
    monkeypatch.setattr(mod.httpx, 'AsyncClient', lambda **kw: Client(payloads(kospi='-0.01')))
    skip, why = asyncio.run(r2.gate('leaderdip'))
    assert skip is False and why == ''


def test_gate_rests_closebet_when_the_won_weakened(monkeypatch):
    """전날 원달러가 올랐으면(원화 약세) 🌙 는 그날 쉰다."""
    import app.regime as mod
    r = Regime(lambda: NOW)
    monkeypatch.setattr(mod.httpx, 'AsyncClient', lambda **kw: Client(payloads(krw=(1330.0, 1331.0))))
    assert asyncio.run(r.gate('closebet'))[0] is True
    r2 = Regime(lambda: NOW)
    monkeypatch.setattr(mod.httpx, 'AsyncClient', lambda **kw: Client(payloads(krw=(1340.0, 1333.0))))
    assert asyncio.run(r2.gate('closebet'))[0] is False


def test_firebar_and_horizonbuy_have_no_gate(monkeypatch):
    """제작자 규칙에 🔥·🌅 맥락 문은 없다."""
    import app.regime as mod
    monkeypatch.setattr(mod.httpx, 'AsyncClient', lambda **kw: Client(payloads()))
    r = Regime(lambda: NOW)
    assert asyncio.run(r.gate('firebar')) == (False, '')
    assert asyncio.run(r.gate('horizonbuy')) == (False, '')
    assert set(GATES) == {'leaderdip', 'closebet'}


def test_gate_does_not_block_when_the_value_cannot_be_read(monkeypatch):
    """맥락을 못 읽으면 막지 않는다 — 문은 기본 규칙에 더한 것이라 자료가 끊겼다고 매매를 바꾸지 않는다."""
    import app.regime as mod

    class Broken:
        async def get(self, *a, **k):
            raise OSError('그물 끊김')

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(mod.httpx, 'AsyncClient', lambda **kw: Broken())
    assert asyncio.run(Regime(lambda: NOW).gate('leaderdip')) == (False, '')
