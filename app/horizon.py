"""관제탑 조건 판정. KRX 자료만 사용하며 주문/가상 체결/성과를 만들지 않는다.

D0는 기준봉 날짜, D+1/2는 코스닥 일봉으로 확인한 거래일이다.
15분봉 시각은 봉 시작 시각. 미완성 봉은 이탈 확인에만 쓰고 통과에는 쓰지 않는다.
원본 코드가 없는 수식(평소=직전20일, 폭=고가/저가-1, 기울기=15분당
첫 종가 대비 OLS %p)은 화면 규칙표에 공개한다.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
import math

from app.models import Candle

KST = timezone(timedelta(hours=9))
VERSION = '2026-09-21.1'


def now_kst():
    return datetime.now(KST)


def pct(a, b):
    return (a / b - 1) * 100 if b and a is not None else None


@dataclass(frozen=True)
class Bar:
    stamp: str  # YYYYMMDDHHMMSS, KRX start time
    open: float
    high: float
    low: float
    close: float
    volume: float = 0


def valid_bar(b):
    return all(math.isfinite(x) and x > 0 for x in (b.open, b.high, b.low, b.close)) and b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high


def baseline(cs: list[Candle], i: int) -> dict | None:
    if i < 20 or not valid_bar(cs[i]) or cs[i].volume <= 0 or cs[i-1].close <= 0:
        return None
    c = cs[i]
    avg = sum(x.amount for x in cs[i-20:i]) / 20
    rise = pct(c.close, cs[i-1].close)
    limit = rise >= 29 - 1e-8 and c.close >= c.high * .995 and c.amount >= 100e8
    explosion = rise >= 15 - 1e-8 and c.amount >= 300e8 and avg > 0 and c.amount >= avg * 5
    if not (limit or explosion):
        return None
    return {'kind': '상한가' if limit else '대금폭발', 'rise': rise, 'amountEok': c.amount / 1e8,
            'multiple': c.amount / avg if avg else None, 'normalAmount': avg}


def window(bars: list[Bar], day: str, start: int, end: int, at: datetime):
    """Closed 15m bars in [start,end), with strict gap checks; start/end are minutes."""
    by = {}
    for b in bars:
        if b.stamp[:8] != day or not valid_bar(b):
            continue
        t = datetime.strptime(b.stamp, '%Y%m%d%H%M%S').replace(tzinfo=KST)
        minute = t.hour * 60 + t.minute
        if start <= minute < end and t + timedelta(minutes=15) <= at:
            by[minute] = b
    expected = set(range(start, end, 15))
    return [by[k] for k in sorted(by)], expected <= by.keys()


def spread(bars):
    return pct(max(b.high for b in bars), min(b.low for b in bars)) if bars else None


def slope(bars):
    if len(bars) < 2:
        return None
    ys = [pct(b.close, bars[0].close) for b in bars]
    xbar = (len(ys)-1)/2
    ybar = sum(ys)/len(ys)
    return sum((i-xbar)*(y-ybar) for i,y in enumerate(ys)) / sum((i-xbar)**2 for i in range(len(ys)))


def ha_ratio(bars):
    """참고 전용, 해당 구간 첫 봉부터 HA를 계산. 판정에 사용하지 않는다."""
    if not bars:
        return None
    op = (bars[0].open + bars[0].close)/2
    good = 0
    for b in bars:
        cl = (b.open+b.high+b.low+b.close)/4
        good += cl >= op
        op = (op+cl)/2
    return good/len(bars)*100


def grade(cap, rs, first_hour_bounds, amount):
    """Missing values never become zero or prove a higher grade. B is a lower bound."""
    big = cap is not None and cap >= 3000
    base = big or amount >= 1000
    if not base:
        return ('확인', '시가총액 확인 필요') if cap is None else ('—', '시총·기준대금 조건 미달')
    if rs is None:
        return 'B', 'B 이상 · 상대강도 확인 필요'
    if rs >= -3 - 1e-8:
        return 'B', '시총 3,000억 또는 기준대금 1,000억 이상'
    if big and first_hour_bounds is not None and first_hour_bounds[1] < 2:
        return 'S', '첫 한 시간 대금 상한도 평소 2배 미만'
    return 'A', 'A 이상 · 첫 한 시간 대금 확인' if big and first_hour_bounds is None else '상대강도 −3%p 미만'


def evaluate(code: str, name: str, theme: str, cs: list[Candle], i: int,
             sessions: list[str], kosdaq: dict[str, float], bars: list[Bar], *, at: datetime,
             cap: float | None = None, restricted: bool | None = None,
             source_exact: bool = True, fetched_at: str | None = None) -> dict:
    at = at.astimezone(KST)
    today, hhmm = at.strftime('%Y%m%d'), at.strftime('%H%M')
    data_at = at
    if fetched_at:
        try:
            data_at = min(at, datetime.fromisoformat(fetched_at))
        except (ValueError, TypeError):
            pass
    d0 = cs[i]
    base = baseline(cs, i)
    if base is None:
        raise ValueError('기준봉 아님')
    sessions = sorted(set(d for d in sessions if d <= today))
    idx = sessions.index(d0.date) if d0.date in sessions else None
    day = len(sessions)-idx-1 if idx is not None else None
    d1date = sessions[idx+1] if idx is not None and len(sessions)>idx+1 else None
    d2date = sessions[idx+2] if idx is not None and len(sessions)>idx+2 else None
    by = {c.date: c for c in cs if c.date <= today}
    d1 = by.get(d1date)
    checks = []
    metrics = {'capEok': cap, 'd1HighPct': None, 'd1Change': None, 'relativeStrength': None,
               'afternoonRange': None, 'afternoonSlope': None, 'morningRange': None,
               'morningLow': None, 'support': d1.low if d1 and d1.low > 0 else None,
               'firstHourBounds': None, 'haMorning': None, 'haAfternoon': None}

    def check(key, title, result, detail):
        checks.append({'key': key, 'title': title, 'state': 'pass' if result is True else 'fail' if result is False else 'pending', 'detail': detail})

    check('exact', '실거래대금', True if source_exact else None, '키움 일봉 거래대금' if source_exact else '추정 거래대금 · 확정 불가')
    check('restriction', '정지·관리·불성실 공시 제외', None if restricted is None else not restricted,
          '종목 상태 조회 필요' if restricted is None else '제외 대상' if restricted else '키움 종목 상태상 해당 없음')
    previous = [j for j in range(max(20, i-10), i) if baseline(cs,j)]
    # 거래정지로 일봉이 누락된 경우 거래일 기준으로 정확히 10일을 확인한다.
    prior_sessions = sessions[max(0, (idx or 0)-10):idx] if idx is not None else []
    previous = [j for j in previous if cs[j].date in prior_sessions]
    coverage = len(prior_sessions) == 10 and all(d in by for d in prior_sessions) and i >= 30
    check('repeat', '앞 10거래일 기준봉 반복 제외', False if previous else True if coverage else None,
          '반복 기준봉 '+', '.join(cs[j].date for j in previous) if previous else '반복 없음' if coverage else '앞 10거래일 확인 자료 부족')
    d0closed = d0.date < today or hhmm >= '1540'
    check('d0close', '기준봉 확정', d0closed or None, d0.date+' 마감 확인' if d0closed else '장중 형성 중')
    d1closed = bool(d1date and (d1date < today or hhmm >= '1540'))
    if d1 and valid_bar(d1):
        metrics['d1HighPct'] = pct(d1.high,d0.close)
        metrics['d1Change'] = pct(d1.close,d0.close)
        if kosdaq.get(d0.date) and kosdaq.get(d1date) and d1closed:
            metrics['relativeStrength'] = metrics['d1Change']-pct(kosdaq[d1date],kosdaq[d0.date])
        shoot = d1.high*100 >= d0.close*115
        check('shoot', 'D+1 고가 +15% 미만', False if shoot else True if d1closed else None,
              f"고가 {metrics['d1HighPct']:+.2f}%"+(' · 장중' if not d1closed else ''))
        af, af_ok = window(bars,d1date,720,930,data_at)
        metrics['haAfternoon'] = ha_ratio(af)
        if af_ok and d1closed:
            # 15:30 종가 단일가가 별도 봉이면 폭/추세에 포함한다.
            af += [b for b in bars if b.stamp[:12] == d1date+'1530' and valid_bar(b)]
            rng, sl = spread(af), slope(af)
            metrics.update(afternoonRange=rng, afternoonSlope=sl)
            check('flat', 'D+1 오후 폭 ≤5% · 기울기', rng <= 5+1e-8 and (abs(sl)<=.1+1e-8 or (sl < -.1 and d1.close>d0.close)),
                  f'폭 {rng:.2f}% · 기울기 {sl:+.3f}%p/봉 · 하락 예외는 D0 종가 위 마감만')
        else:
            check('flat','D+1 오후 폭 ≤5% · 기울기',None,'12:00~15:30 완성 15분봉 대기 / 누락 확인')
    else:
        check('shoot','D+1 고가 +15% 미만',None,'D+1 일봉 대기')
        check('flat','D+1 오후 횡보',None,'D+1 오후 자료 대기')

    morning = []
    morning_ok = False
    if d2date:
        cutoff = 720 if d2date < today or hhmm >= '1200' else 660
        morning, morning_ok = window(bars,d2date,540,cutoff,data_at)
        observed = [b for b in bars if b.stamp[:8] == d2date and '090000'<=b.stamp[8:]<'120000'
                    and b.stamp <= at.strftime('%Y%m%d%H%M%S') and valid_bar(b)]
        low = min((b.low for b in observed),default=None)
        metrics['morningLow'] = low
        broken = low is not None and metrics['support'] is not None and low < metrics['support']
        check('support','D+2 오전 D+1 저가 유지', False if broken else True if morning_ok and metrics['support'] else None,
              '한 번이라도 이탈 · 회복해도 퇴출' if broken else '저가 유지' if morning_ok and metrics['support'] else '오전 전체 봉 확인 필요')
        rng = spread(morning)
        metrics.update(morningRange=rng,haMorning=ha_ratio(morning))
        check('range','D+2 오전 폭 ≥4%', (rng >= 4-1e-8) if morning_ok else None,
              f'폭 {rng:.2f}%'+(' · 미완성' if not morning_ok else '') if rng is not None else '오전 자료 대기')
        hour, hour_ok = window(bars,d2date,540,600,data_at)
        if hour_ok and base['normalAmount'] > 0:
            # 체결 대금이 없는 OHLCV를 정확한 대금처럼 쓰지 않고 가능한 하한/상한으로 비교.
            metrics['firstHourBounds'] = [sum(b.volume*b.low for b in hour)/base['normalAmount'],
                                          sum(b.volume*b.high for b in hour)/base['normalAmount']]
    else:
        check('support','D+2 오전 D+1 저가 유지',None,'D+2 대기')
        check('range','D+2 오전 폭 ≥4%',None,'D+2 대기')

    g, gnote = grade(cap,metrics['relativeStrength'],metrics['firstHourBounds'],base['amountEok'])
    check('grade','S·A·B 범위', None if g=='확인' else g in ('S','A','B'),gnote)
    # 현재 거래일과 데이터 최신성은 매수 후보 표시의 필요조건.
    fresh = False
    if fetched_at:
        try:
            age = (at-datetime.fromisoformat(fetched_at)).total_seconds()
            fresh = 0 <= age <= 180
        except (ValueError,TypeError):
            pass
    failed = [c for c in checks if c['state']=='fail']
    pending = [c for c in checks if c['state']=='pending']
    if failed:
        status, label = 'excluded','퇴출' if any(c['key']=='support' for c in failed) else '제외'
    elif not sessions or sessions[-1] != today:
        status, label = 'review','지난 자료'
    elif day == 0:
        status, label = 'baseline','기준봉' if d0closed else '기준봉 형성 중'
    elif day == 1:
        status, label = 'watch','D+1 관찰'
    elif day == 2 and hhmm < '1100':
        status, label = 'waiting','오전 확인 중'
    elif day == 2 and pending:
        status, label = 'incomplete','확인 필요'
    elif day == 2 and not fresh:
        status, label = 'incomplete','최신 자료 필요'
    elif day == 2 and hhmm < '1200':
        status, label = 'preview','11시 미리보기'
    elif day == 2 and hhmm == '1200':
        status, label = 'ready','정오 조건 충족'
    else:
        status, label = 'review','정오 판정 기록' if day is not None and day >= 2 else '거래일 확인'
    prior = cs[max(0,i-250):i]
    avg_range = sum(pct(c.high,c.low) for c in prior if valid_bar(c))/sum(valid_bar(c) for c in prior) if any(valid_bar(c) for c in prior) else None
    run = 0
    for j in range(i-1,0,-1):
        if cs[j].close <= cs[j-1].close:
            break
        run += 1
    before20 = pct(cs[i-1].close,cs[i-21].close) if i>=21 else None
    before60 = pct(cs[i-1].close,cs[i-61].close) if i>=61 else None
    warnings = []
    if (before20 is not None and before20>=40) or run>=5 or (before60 is not None and before60>=100):
        warnings.append('기준봉 전 과열 · 제외 조건 아님, 손절 기준 확인')
    if metrics['morningLow'] and metrics['support'] and pct(metrics['morningLow'],metrics['support'])>5:
        warnings.append('오전 저가가 지지선보다 5% 넘게 높음 · 참고')
    metrics.update(avgRange250=avg_range,rangeSample=len(prior),pre20=before20,pre60=before60,upStreak=run)
    return {'id':code+'-'+d0.date,'code':code,'name':name,'theme':theme,'d0':d0.date,'d1':d1date,'d2':d2date,
            'day':day,'base':base,'baseClose':d0.close,'grade':g,'gradeNote':gnote,'status':status,'label':label,
            'eligible':status=='ready','checks':checks,'metrics':metrics,'warnings':warnings,
            'reason':failed[0]['detail'] if failed else pending[0]['detail'] if pending else label,
            'fetchedAt':fetched_at,'price':None,'change':None,
            'chart':[asdict(b) for b in bars if b.stamp[:8] in (d1date,d2date) and b.stamp<=at.strftime('%Y%m%d%H%M%S')]}


RULES = [
    ('진입','기준봉','상한가형: 종가 +29% 이상·고가의 99.5% 이상·대금 100억 이상. 대금폭발형: +15% 이상·300억 이상·직전 20일 평균의 5배 이상.'),
    ('제외','반복 기준봉','D0 앞 10거래일 안에 기준봉이 또 있으면 등급과 무관하게 제외.'),
    ('진입','D+1','고가가 D0 종가 대비 +15% 미만. 오후 12시 이후 폭 ≤5%, |기울기| ≤0.1. 하락 기울기는 D+1 종가가 D0 종가보다 높으면 허용. 상승 기울기 초과는 제외.'),
    ('제외','D+2 저가','09:00~12:00 중 D+1 저가 미만을 한 번이라도 기록하면 퇴출. 동일 가격은 유지. 다시 회복해도 퇴출 유지.'),
    ('진입','D+2 오전 폭','오전 (최고가/최저가−1)×100 ≥4%. 11시는 미리보기, 12시까지 전부 다시 확인.'),
    ('등급','★★★ S','시총 ≥3,000억 + D+1 상대강도 <−3%p + 첫 1시간 대금 <직전20일 평균 일대금의 2배.'),
    ('등급','★★ A','시총 ≥3,000억 또는 기준대금 ≥1,000억, 그리고 D+1 상대강도 <−3%p.'),
    ('등급','★ B','시총 ≥3,000억 또는 기준대금 ≥1,000억. HA 비율은 등급·진입에서 제외. S→A→B 순으로 최고 등급 하나를 표시.'),
    ('진입','정오','기준봉 D+2 12:00 조건 충족 표시. 지나간 시간은 기록으로 표시하며 늦은 추격 진입으로 바꾸지 않음. 자동매수 없음.'),
    ('청산','사용자 설정','실제 매수일을 별도 D0로 계산. 평단 +15% 전량 / −10% 전량 / 매수 후 3거래일 종가. 비용 전 가격 기준. 실제 주문은 꺼짐.'),
    ('청산','미적용 참고','제작자 2일 보유·청산일 본전 지정가→종가, 과거 5~7일 계획은 참고. 사용자 3거래일 설정과 섞지 않음. 추적매도 미설정.'),
    ('계산','상대강도·기울기','상대강도=D+1 종목 종가 수익률−같은 날 코스닥 수익률. 기울기=15분 종가를 첫 종가 대비 %로 환산한 회귀 기울기(%p/봉). 원본 수식 미제공에 따른 구현 정의.'),
    ('계산','대금·시총','시총은 조회 시점 값. 분봉 대금은 OHLCV의 저가×거래량~고가×거래량 범위로 확인. 상한이 2배 미만일 때만 S 인정. 범위가 문턱을 걸치면 A 유지.'),
    ('제외','종목 상태','키움 종목 상태의 거래정지·관리종목·불성실 공시 표시를 제외. 응답 누락은 확인 필요. 공시 원문 전수 검증은 별도 확인 필요.'),
    ('참고','하이킨아시·과열','HA 15분 양봉 비율, 이전 20일 +40%·60일 +100%·5일 연속 상승, 지지선에서 5% 이상 이격을 참고 표시. 이 정보로 등급을 낮추거나 제외하지 않음.'),
    ('참고','움직임·반복','기준봉 전 최대250일 평균 일중폭과 표본 수 표시. 평균폭 6~9%, 기준봉 빈도 등은 제작자 연구 방향이며 별도 진입 조건 아님.'),
    ('참고','재료·공시','정책·정부 재료, 계약·수주, 실적·IR, 유상증자·CB·조회공시·투자경고의 연구 방향은 참고용. 뉴스 강도를 자동 점수화하거나 성공 확률로 환산하지 않음.'),
    ('참고','시간외·CVD','NXT는 정규장 판정에 포함하지 않음. CVD는 분봉으로 복원하지 않으며 자료 없는 값은 표시하지 않음.'),
    ('미적용','대안 전략','구조 손절·HA 추세매도·박스 돌파·피뢰침·손절선 근처 미수 물타기·다음날 +5% 청산은 추가하지 않음.'),
    ('성과','제작자 참고 수치','제공자료: S 50건 PF3.86 / S+A 72건 PF3.20 / S+A+B 143건 PF1.83 / 상한가·갭 제외 111건 PF1.61. 기간·계획·표본이 다른 통계이며 직접 재검증하지 않음. 이 앱의 3거래일 성과가 아님.'),
]
