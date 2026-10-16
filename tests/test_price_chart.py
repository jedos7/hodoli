"""Chart reads use a fake quote client and cannot reach an account/order API."""
import asyncio
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from app.autosell import KST
from app.price_chart import PriceChartService, create_price_chart_router, parse_chart

NOW = datetime(2026, 9, 23, 11, 30, tzinfo=KST)


def row(stamp='20260923112900', **changes):
    return dict(cntr_tm=stamp, dt=stamp[:8], open_pric='-83800', high_pric='+84000', low_pric='83000', cur_prc='+83900', trde_qty='1,200') | changes


class Quotes:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def post(self, path, api_id, payload):
        assert path == '/api/dostk/chart'
        assert api_id in ('ka10080', 'ka10081')
        self.calls.append((path, api_id, payload))
        if self.fail: raise RuntimeError('Fake quote failure')
        await asyncio.sleep(0)
        key = 'stk_dt_pole_chart_qry' if api_id == 'ka10081' else 'stk_min_pole_chart_qry'
        return {key: [row()]}, ''


def test_parsing_orders_deduplicates_and_filters_bad_or_non_krx_bars():
    source = [row(), row('20260923112800'), row(cur_prc='83850'), row('20260923085900'),
        row('20260923153100'), row('20260924090000'), row('20260230090000'),
        row('20260923120000'), row('20260923112500', cur_prc='NaN'),
        row('20260923112400', high_pric='82000'), row('20260923112300', low_pric='0'),
        row('20260923112200', trde_qty='Infinity'), 'invalid']
    bars = parse_chart({'stk_min_pole_chart_qry': source}, '1m', NOW)
    assert [b['stamp'] for b in bars] == ['20260923112800', '20260923112900']
    assert bars[-1]['close'] == 83850 and bars[-1]['volume'] == 1200
    assert bars[-1]['open'] == 83800


def test_daily_and_empty_responses_are_not_synthetic():
    bars = parse_chart({'stk_dt_pole_chart': [row(), row('20260922090000')]}, '1d', NOW)
    assert [b['stamp'] for b in bars] == ['20260922', '20260923']
    assert parse_chart({'stk_min_pole_chart_qry': []}, '1m', NOW) == []
    with pytest.raises(ValueError): parse_chart({'other': []}, '1m', NOW)


@pytest.mark.parametrize('interval,api,tick', [('1m', 'ka10080', '1'), ('5m', 'ka10080', '5'), ('15m', 'ka10080', '15'), ('1d', 'ka10081', None)])
def test_correct_quote_api_and_source(interval, api, tick):
    async def run():
        rest = Quotes()
        service = PriceChartService(lambda: rest, clock=lambda: NOW)
        result = await service.read('126340', interval)
        assert result['code'] == '126340' and result['interval'] == interval
        assert result['fetched_at'] == NOW.isoformat(timespec='seconds')
        assert result['last_bar_at'] == ('20260923' if interval == '1d' else '20260923112900')
        assert rest.calls == [('/api/dostk/chart', api,
            {'stk_cd': '126340', 'upd_stkpc_tp': '1'} | ({'tic_scope': tick} if tick else {'base_dt': '20260923'}))]
    asyncio.run(run())


def test_cache_deduplicates_concurrent_refresh_and_expires():
    async def run():
        rest = Quotes(); timer = [0]
        service = PriceChartService(lambda: rest, clock=lambda: NOW, timer=lambda: timer[0])
        results = await asyncio.gather(*(service.read('126340', '1m') for _ in range(6)))
        assert len(rest.calls) == 1 and len(results) == 6
        timer[0] = 14.9
        await service.read('126340', '1m'); assert len(rest.calls) == 1
        timer[0] = 15
        await service.read('126340', '1m'); assert len(rest.calls) == 2
        await service.read('126340', '5m'); assert len(rest.calls) == 3
        await service.read('005930', '1m'); assert len(rest.calls) == 4
    asyncio.run(run())


def test_lost_connection_or_failed_refresh_never_claims_fresh_cached_data():
    async def run():
        rest = Quotes(); current = [rest]; timer = [0]
        service = PriceChartService(lambda: current[0], clock=lambda: NOW, timer=lambda: timer[0])
        await service.read('126340', '1m')
        current[0] = None
        with pytest.raises(RuntimeError): await service.read('126340', '1m')
        replacement = Quotes(); replacement.fail = True; current[0] = replacement
        with pytest.raises(RuntimeError): await service.read('126340', '1m')
        current[0] = rest; timer[0] = 16; rest.fail = True
        with pytest.raises(RuntimeError): await service.read('126340', '1m')
        assert service.cache[('126340', '1m')][1] == 0
    asyncio.run(run())


def test_chart_api_validation_local_access_and_read_only():
    async def run():
        rest = Quotes()
        app = FastAPI(); app.include_router(create_price_chart_router(PriceChartService(lambda: rest, clock=lambda: NOW)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as client:
            good = await client.get('/api/price-chart/126340?interval=1m&limit=240')
            assert good.status_code == 200 and good.headers['cache-control'] == 'no-store'
            for path in ('126340?interval=2m', '126340?limit=5000', '126340?limit=0', '126340_NX'):
                assert (await client.get('/api/price-chart/'+path)).status_code == 422
            assert (await client.get('/api/price-chart/126340', headers={'Origin':'https://elsewhere.test'})).status_code == 403
            assert (await client.get('/api/price-chart/126340', headers={'Host':'elsewhere.test'})).status_code == 403
            assert (await client.post('/api/price-chart/126340')).status_code == 405
            rest.fail = True
            bad = await client.get('/api/price-chart/005930')
            assert bad.status_code == 502
            assert 'Fake' not in bad.text
        assert all(path == '/api/dostk/chart' for path, _, _ in rest.calls)
    asyncio.run(run())
