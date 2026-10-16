"""Analytics only: fixtures cannot call an account or order endpoint."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.autosell import KST
from app.kiwoom.parse import parse_real
from app.stock_flow import (TradeCapture, WallCapture, StockFlowService, create_stock_flow_router,
                            parse_program, parse_brokers)

NOW = datetime(2026, 9, 23, 10, 0, tzinfo=KST)


def tick(q='+10', acc=10, price='-12000', market=''):
    return parse_real({'trnm': 'REAL', 'data': [{'type': '0B', 'item': '126340'+market,
        'values': {'10': price, '15': q, '13': str(acc), '20': '100000', '12': '-2'}}]})[0]


def test_signed_side_is_independent_of_price_change_and_existing_volume():
    buy, sell, invalid = tick(), tick('-7', 17, '+12010'), tick('--8')
    assert (buy.price, buy.volume, buy.signed_volume, buy.change_rate) == (12000, 10, 10, -2)
    assert sell.volume == 7 and sell.signed_volume == -7
    assert invalid.signed_volume is None


def test_price_levels_cvd_dedup_and_partial_start(tmp_path):
    c = TradeCapture(tmp_path/'capture.json', lambda: NOW)
    c.on_trade(tick(acc=100), 1)
    c.on_trade(tick(acc=100), 1)
    c.on_trade(tick('-7', 107, '+12010'), 1)
    c.on_trade(tick('+2', 109, '12010'), 1)
    c.on_trade(tick(acc=100), 1)  # Late packet.
    view = c.view('126340', '20260923', ['20260923'])
    assert view['ticks'] == 3 and view['poc'] == 12000
    assert view['received'][0]['net'] == 10 and view['sold'][0]['net'] == -5
    assert view['total_won'] == 120000 - 5*12010
    assert view['partial'] and not view['complete'] and view['ratio_pct'] is None


def test_no_backfill_or_cross_venue_session_capture(tmp_path):
    c = TradeCapture(tmp_path/'c.json', lambda: NOW)
    for t in (tick(market='_NX'), tick(market='_AL'), tick('--3')):
        c.on_trade(t)
    late = tick(); late.time='095900'; c.on_trade(late)
    future = tick(); future.time='100010'; c.on_trade(future)
    overnight = tick(); overnight.time='190000'; c.on_trade(overnight)
    assert not c.days
    c.on_trade(tick())
    v = c.view('126340', '20260921', ['20260921','20260922','20260923'])
    assert v['base_won'] is None and not v['complete'] and v['ratio_pct'] is None
    assert len(v['daily']) == 1


@pytest.mark.parametrize('acc,generation', [(25,1), (20,2)])
def test_missing_packets_or_reconnect_never_claim_complete(tmp_path, acc, generation):
    c = TradeCapture(tmp_path/'c.json', lambda: NOW)
    c.on_trade(tick(), 1)
    assert c.view('126340','20260923',['20260923'])['complete']
    c.on_trade(tick(acc=acc), generation)
    assert not c.view('126340','20260923',['20260923'])['complete']


def test_persist_restart_and_cross_day_boundaries(tmp_path):
    now = [NOW]
    c = TradeCapture(tmp_path/'c.json', lambda: now[0])
    c.on_trade(tick(), 1)
    asyncio.run(c.flush())
    loaded = TradeCapture(c.path, lambda: now[0])
    loaded.on_trade(tick(), 1)
    assert loaded.view('126340','20260923',[])['ticks'] == 1
    assert loaded.view('126340','20260923',[])['partial']
    now[0] += timedelta(days=1)
    loaded.on_trade(tick('-10'), 1)
    v = loaded.view('126340','20260923',['20260923','20260924'])
    assert len(v['daily']) == 2 and v['total_won'] == 0
    now[0] += timedelta(days=50); loaded.prune(); assert not loaded.days


def orderbook(stamp='100000', qty=100):
    body = {'bid_req_base_tm':stamp}
    for level in range(1,11):
        prefix='sel_fpr' if level == 1 else f'sel_{level}th_pre'
        body[prefix+'_bid']=str(12000+level*10)
        body[prefix+'_req']=str(qty)
    return body


def test_wall_samples_do_not_count_cached_requests_and_reset_after_gap():
    wall=WallCapture()
    first=wall.update(orderbook(),NOW)
    assert first['rows'][0]['samples']==1
    assert wall.update(orderbook(),NOW)['rows'][0]['samples']==1
    second=wall.update(orderbook('100020',200),NOW+timedelta(seconds=20))
    assert second['rows'][0]['samples']==2 and second['rows'][0]['average']==150
    after=wall.update(orderbook('100200'),NOW+timedelta(seconds=120))
    assert after['rows'][0]['samples']==1
    with pytest.raises(ValueError):wall.update(orderbook('090000'),NOW)
    broken=orderbook(); del broken['sel_10th_pre_req']
    with pytest.raises(ValueError):WallCapture().update(broken,NOW)


def program_row(**kwargs):
    return dict(tm='095959',stex_tp='KRX',prm_buy_amt='1,230',prm_sell_amt='1000',prm_netprps_amt='+230',
                prm_buy_qty='2000',prm_sell_qty='1500',prm_netprps_qty='+500')|kwargs


def test_program_million_won_and_share_units_unknown_and_zero():
    p = parse_program({'stk_tm_prm_trde_trnsn':[program_row(),program_row(tm='100010'),program_row(stex_tp='NXT')]}, '20260923',NOW)
    assert p['net_won'] == 230_000_000 and p['net'] == 500 and p['time']=='095959'
    zero=parse_program({'stk_tm_prm_trde_trnsn':[program_row(prm_netprps_amt='0',prm_sell_qty='')]},'20260923',NOW)
    assert zero['net_won']==0 and zero['sell'] is None
    with pytest.raises(ValueError):parse_program({'stk_tm_prm_trde_trnsn':[program_row(stex_tp='통합')]},'20260923',NOW)
    with pytest.raises(ValueError):parse_program({'stk_tm_prm_trde_trnsn':[program_row(prm_netprps_amt='NaN')]},'20260923',NOW)


def broker_row(name='테스트증권',buy=100,sell=20):
    return dict(mmcm_nm=name,buy_qty=str(buy),sell_qty=str(sell),acc_netprps_qty=str(buy-sell))


def test_brokers_must_have_both_sides_and_consistent_net():
    result=parse_brokers({'stk_sec_rank':[broker_row(),broker_row()|{'sell_qty':''},broker_row()|{'acc_netprps_qty':'90'}]})
    assert result == [dict(name='테스트증권',buy=100,sell=20,net=80)]


def test_live_broker_direction_signs_and_duplicate_minus():
    rows=[dict(mmcm_nm='KB증권',buy_qty='+48484',sell_qty='-229903',acc_netprps_qty='--181419'),
          dict(mmcm_nm='JP모간서울',buy_qty='+314433',sell_qty='-60048',acc_netprps_qty='+254385')]
    result=parse_brokers({'stk_sec_rank':rows})
    assert result==[dict(name='KB증권',buy=48484,sell=229903,net=-181419),
                    dict(name='JP모간서울',buy=314433,sell=60048,net=254385)]
    with pytest.raises(ValueError):parse_brokers({'stk_sec_rank':[rows[0]|{'acc_netprps_qty':'+-181419'}]})


class Feed:
    def __init__(self):
        self.calls=[]; self.subscribed=[]; self.rest=self; self.ws=SimpleNamespace(connected=True)
        self.fail=False
    async def watch_codes(self,codes):self.subscribed.extend(codes)
    async def post(self,path,api,body):
        assert api in ('ka20006','ka10004','ka90008','ka10038')
        assert path in ('/api/dostk/chart','/api/dostk/mrkcond','/api/dostk/rkinfo')
        self.calls.append((api,body))
        if self.fail and api=='ka90008':raise RuntimeError('sensitive upstream response')
        if api=='ka20006':
            # Holiday/weekend gap: choose actual returned sessions, never calendar subtraction.
            return {'inds_dt_pole_qry':[{'dt':d,'trde_qty':'100'} for d in ('20260923','20260918')]},''
        if api=='ka10004':return orderbook(),''
        if api=='ka90008':return {'stk_tm_prm_trde_trnsn':[program_row()]},''
        if body['strt_dt']==body['end_dt']=='20260918':rs=[broker_row('전환증권',buy=100,sell=0)]
        elif body['strt_dt']==body['end_dt']=='20260923':rs=[broker_row('전환증권',buy=0,sell=50)]
        else:rs=[broker_row(),broker_row('매도증권',buy=0,sell=100)]
        return {'stk_sec_rank':rs},'MORE'


def test_service_only_quote_apis_session_dates_flip_and_cache(tmp_path):
    async def run():
        feed=Feed(); now=[0]
        s=StockFlowService(tmp_path/'c.json',lambda:feed,clock=lambda:NOW,timer=lambda:now[0])
        a,b=await asyncio.gather(s.read('126340','20260918'),s.read('126340','20260918'))
        assert len(feed.calls)==9
        assert a['brokers']['data']['days']==['20260918','20260923']
        assert a['brokers']['data']['flips']==[dict(name='전환증권',before=100,now=-50)]
        assert a['brokers']['data']['bought'][0]['net']==80  # Never double summed.
        assert a['brokers']['data']['partial']
        feed.fail=True; now[0]=61
        result=await s.read('126340','20260918')
        assert result['program']['error'] and result['program']['fetched_at']==a['program']['fetched_at']
        assert 'sensitive' not in result['program']['error']
        assert result['program']['data']==a['program']['data']
        assert not result['brokers']['error']
    asyncio.run(run())


def test_no_connection_does_not_create_fake_data(tmp_path):
    s=StockFlowService(tmp_path/'c.json',lambda:None,clock=lambda:NOW)
    r=asyncio.run(s.read('126340','20260918'))
    assert r['capture']['total_won'] is None and r['program']['data'] is None
    assert not r['connected']


def test_route_origin_date_and_readonly_guard(tmp_path):
    async def run():
        feed=Feed(); service=StockFlowService(tmp_path/'c.json',lambda:feed,clock=lambda:NOW)
        app=FastAPI(); app.include_router(create_stock_flow_router(service))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as c:
            url='/api/horizon-flow/126340?since=20260918'
            result=await c.get(url)
            assert result.status_code==200 and result.headers['cache-control']=='no-store'
            assert (await c.post(url)).status_code==405
            assert (await c.get(url,headers={'Origin':'https://bad.test'})).status_code==403
            assert (await c.get(url,headers={'Host':'bad.test'})).status_code==403
            for bad in ('126340?since=20260924','126340?since=20260230','bad?since=20260918','126340?since=unknown'):
                assert (await c.get('/api/horizon-flow/'+bad)).status_code==422
    asyncio.run(run())
