from datetime import datetime, timedelta
from dataclasses import replace
from types import SimpleNamespace
import asyncio
import pytest

from app.horizon import Bar, KST, baseline, evaluate, grade, window
from app.horizon_service import HorizonService, parse_minutes
from app.models import Candle


def fixture():
    days=[]
    d=datetime(2026,7,20,tzinfo=KST)
    while len(days)<36:
        if d.weekday()<5:days.append(d.strftime('%Y%m%d'))
        d+=timedelta(days=1)
    cs=[Candle(d,100,101,99,100,1000000,int(100e8)) for d in days]
    cs[32]=Candle(days[32],100,130,99,130,10000000,int(1200e8))
    cs[33]=Candle(days[33],130,132,127,128,1000000,int(200e8))
    at=datetime.strptime(days[34]+'120020','%Y%m%d%H%M%S').replace(tzinfo=KST)
    bars=[Bar(days[33]+f'{minute//60:02}{minute%60:02}00',128,129,127,128,100000) for minute in range(720,930,15)]
    bars += [Bar(days[34]+f'{minute//60:02}{minute%60:02}00',128,134,127,130,100000) for minute in range(540,720,15)]
    return dict(code='005930',name='시험종목',theme='시험',cs=cs,i=32,sessions=days,kosdaq={days[32]:100,days[33]:102},
                bars=bars,at=at,cap=3000,restricted=False,fetched_at=at.isoformat())


def result(**changes):
    kw=fixture();kw.update(changes);return evaluate(**kw)


def test_exact_noon_S_and_full_checklist():
    r=result()
    assert r['eligible'] and r['grade']=='S'
    assert all(c['state']=='pass' for c in r['checks'])
    assert r['day']==2


@pytest.mark.parametrize('hour,minute,label',[(10,30,'오전 확인 중'),(11,0,'11시 미리보기'),(11,59,'11시 미리보기'),(12,0,'정오 조건 충족'),(12,1,'정오 판정 기록'),(13,0,'정오 판정 기록')])
def test_timing_no_early_or_late_buy(hour,minute,label):
    f=fixture();at=f['at'].replace(hour=hour,minute=minute)
    r=result(at=at,fetched_at=at.isoformat())
    assert r['label']==label
    assert r['eligible']==(hour==12 and minute==0)


def test_breach_overrides_S_and_recovery():
    f=fixture();b=f['bars'];b[-10]=replace(b[-10],low=126)
    r=result(bars=b)
    assert r['grade']=='S' and r['status']=='excluded' and not r['eligible']
    assert '회복' in r['reason']


def test_support_touch_is_not_breach():
    assert result()['metrics']['morningLow']==result()['metrics']['support']
    assert result()['eligible']


def test_after_noon_breach_does_not_change_morning_decision():
    f=fixture();b=f['bars']+[Bar(f['at'].strftime('%Y%m%d')+'130000',130,130,110,115,1)]
    r=result(bars=b,at=f['at'].replace(hour=14))
    assert next(c for c in r['checks'] if c['key']=='support')['state']=='pass'
    assert not r['eligible']


def test_preview_must_recheck_last_hour():
    f=fixture();b=f['bars'];b[-1]=replace(b[-1],low=126)
    at=f['at'].replace(hour=11,minute=0,second=1)
    assert result(bars=b,at=at,fetched_at=at.isoformat())['status']=='preview'
    assert result(bars=b)['status']=='excluded'


def test_missing_bar_blocks_entry_and_stale_quote_blocks_entry():
    f=fixture()
    assert result(bars=f['bars'][:-1])['status']=='incomplete'
    assert not result(fetched_at=(f['at']-timedelta(minutes=4)).isoformat())['eligible']
    assert not result(fetched_at=(f['at']+timedelta(seconds=1)).isoformat())['eligible']
    assert not result(restricted=None)['eligible']
    assert not result(source_exact=False)['eligible']
    # 11:59에 받은 11:45 봉이 벽시계 12:00에 저절로 확정되지 않는다.
    assert not result(fetched_at=f['at'].replace(hour=11,minute=59).isoformat())['eligible']


def test_morning_range_four_percent_boundary():
    f=fixture();b=[replace(x,high=132.08,low=127) if x.stamp[:8]==f['at'].strftime('%Y%m%d') else x for x in f['bars']]
    assert result(bars=b)['eligible']
    b=[replace(x,high=132.07) if x.stamp[:8]==f['at'].strftime('%Y%m%d') else x for x in b]
    assert result(bars=b)['status']=='excluded'


def test_D1_shooting_exact_15_is_excluded():
    f=fixture();f['cs'][33]=replace(f['cs'][33],high=149.5)
    assert result(cs=f['cs'])['status']=='excluded'


def test_repeated_baseline_in_prior_ten_sessions_rejected():
    f=fixture();f['cs'][22]=Candle(f['cs'][22].date,100,130,99,130,1000000,int(100e8))
    assert result(cs=f['cs'])['status']=='excluded'
    f=fixture();f['cs'][21]=Candle(f['cs'][21].date,100,130,99,130,1000000,int(100e8))
    assert result(cs=f['cs'])['eligible']


def test_absent_daily_session_does_not_shift_D2():
    f=fixture();f['cs'].pop(33)
    r=result(cs=f['cs'])
    assert not r['eligible'] and r['day']==2 and r['metrics']['support'] is None


def test_trading_days_count_weekends_and_holidays_from_calendar():
    f=fixture();holiday=f['sessions'][34]
    # 실제 세션 목록에서 휴장을 제거. 달력 평일로 D+2를 미리 정하지 않는다.
    at=f['at']
    r=result(sessions=[d for d in f['sessions'] if d!=holiday],at=at)
    assert r['day']==1 and not r['eligible']


def test_partial_baseline_never_eligible():
    f=fixture();at=datetime.strptime(f['cs'][32].date+'120000','%Y%m%d%H%M%S').replace(tzinfo=KST)
    r=result(at=at,fetched_at=at.isoformat())
    assert not r['eligible'] and r['label']=='기준봉 형성 중'


@pytest.mark.parametrize('cap,rs,bounds,amount,expected',[(3000,-3.01,[1,1.99],500,'S'),(3000,-3,[1,1.9],500,'B'),(2999,-4,[1,1.9],1000,'A'),(2999,-2,None,999,'—'),(None,None,None,1000,'B'),(None,None,None,999,'확인'),(3000,-4,[1.99,2.01],500,'A'),(3000,-4,[2,2],500,'A')])
def test_grade_boundaries_and_missing_data(cap,rs,bounds,amount,expected):
    assert grade(cap,rs,bounds,amount)[0]==expected


def test_downward_exception_only_above_D0_close():
    f=fixture();day=f['cs'][33].date
    bars=[Bar(day+f'{minute//60:02}{minute%60:02}00',134-n*.15,134-n*.15,133.8-n*.15,134-n*.15,1) for n,minute in enumerate(range(720,930,15))]
    bars += [x for x in f['bars'] if x.stamp[:8]!=day]
    f['cs'][33]=replace(f['cs'][33],high=135,close=132)
    r=result(cs=f['cs'],bars=bars)
    assert next(c for c in r['checks'] if c['key']=='flat')['state']=='pass'
    f['cs'][33]=replace(f['cs'][33],close=130)
    r=result(cs=f['cs'],bars=bars)
    assert next(c for c in r['checks'] if c['key']=='flat')['state']=='fail'


def test_parser_filters_extended_hours_and_bad_values():
    def raw(t,low='-100'):
        return dict(cntr_tm='20260921'+t,open_pric='+101',high_pric='102',low_pric=low,cur_prc='101',trde_qty='10')
    bars=parse_minutes({'stk_min_pole_chart_qry':[raw('080000'),raw('090000'),raw('091500','0'),raw('153000'),raw('154500')]})
    assert [b.stamp[8:] for b in bars]==['090000','153000']
    assert bars[0].low==100
    with pytest.raises(ValueError):parse_minutes({})


def test_service_restore_and_no_fabricated_today(tmp_path):
    f=fixture()
    s=HorizonService(SimpleNamespace(data_dir=tmp_path),lambda:None,lambda:SimpleNamespace(theme_of={},themes=[]))
    s.loaded=True;s.daily={'005930':f['cs']};s.names={'005930':['시험','시험']}
    s.save()
    t=HorizonService(SimpleNamespace(data_dir=tmp_path),lambda:None,lambda:SimpleNamespace(theme_of={},themes=[]))
    t.load()
    assert len(t.daily['005930'])==36
    d=t.snapshot(at=f['at'])
    assert not d['ordersEnabled'] and not d['autoBuy']
    assert all(not r['eligible'] for r in d['rows'])


def test_minutes_paginates_using_read_only_api(tmp_path):
    class Rest:
        calls=[]
        async def post(self,path,api,body,nk):
            self.calls.append((api,body,nk))
            day='20260921' if not nk else '20260917'
            return {'stk_min_pole_chart_qry':[dict(cntr_tm=day+'090000',open_pric='10',high_pric='12',low_pric='9',cur_prc='11',trde_qty='3')]},'next' if not nk else ''
    s=HorizonService(SimpleNamespace(data_dir=tmp_path),lambda:None,lambda:None)
    rest=Rest();bars=asyncio.run(s.minutes(rest,'005930','20260918'))
    assert len(bars)==1 and len(rest.calls)==2
    assert all(api=='ka10080' and body['stk_cd']=='005930' for api,body,_ in rest.calls)


def test_universe_outage_does_not_stop_existing_watch(tmp_path,monkeypatch):
    from app.collectors import naver_daily
    class Daily:
        async def market_list(self,**kwargs):raise ConnectionError('offline')
        async def close(self):pass
    class Rest:
        async def daily_candles(self,code,count):return fixture()['cs']
    monkeypatch.setattr(naver_daily,'NaverDaily',Daily)
    s=HorizonService(SimpleNamespace(data_dir=tmp_path),lambda:None,lambda:SimpleNamespace(themes=[]))
    s.names={'005930':['시험','시험']}
    asyncio.run(s.scan(Rest()))
    assert s.daily['005930'] and s.scan_at and '기존 범위' in s.scope_warning
