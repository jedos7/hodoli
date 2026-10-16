"""Read-only research: explicit coverage, dated sources, no trading-engine dependencies."""
import asyncio
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from app.collectors.naver_research import NaverResearch, parse_list


def aggregate(reports, today, complete):
    # Recent 28 calendar days versus the preceding 62 days, normalized to 28 days.
    since=today-timedelta(days=89); recent=today-timedelta(days=27)
    unique={}
    for r in reports:
        try: day=date.fromisoformat(r.date)
        except ValueError: continue
        if since<=day<=today: unique[r.nid]=r
    groups=defaultdict(list)
    for r in unique.values(): groups[r.category or '분류 미제공'].append(r)
    out=[]
    for category,rows in groups.items():
        n=sum(r.date>=recent.isoformat() for r in rows); prior=len(rows)-n
        base=prior/62*28
        out.append(dict(industry=category,count=len(rows),recent=n,previous=prior,baseline=round(base,2),
                        ratio=round(n/base,2) if complete and prior else None,
                        six_month=None,one_year=None,
                        reports=[dict(id=r.nid,title=r.title,broker=r.broker,date=r.date,url=r.url)
                                 for r in sorted(rows,key=lambda r:(r.date,r.nid),reverse=True)]))
    out.sort(key=lambda x:(x['ratio'] is not None,x['ratio'] or 0,x['recent']),reverse=True)
    return dict(asof=datetime.now().isoformat(timespec='seconds'),since=since.isoformat(),until=today.isoformat(),
                recent_since=recent.isoformat(),complete=complete,total=len(unique),industries=out,
                baseline_days=62,recent_days=28,source='네이버 증권 · 증권사 산업 리포트 목록',
                note='최근 28일 ÷ 이전 62일의 28일 환산 건수. 사진의 평소 산식이 공개되지 않아 별도로 명시한 기준입니다.')


def classify(row):
    """Normalized single-quarter observations only; absent inputs are never passes."""
    quarters=row.get('quarters',[])
    required=['revenue','revenue_prev_year','operating_profit','operating_profit_prev_year']
    if len(quarters)<3 or any(any(not isinstance(q.get(k),(int,float)) for k in required) for q in quarters[-3:]):
        return {'growth':None,'fallen':None,'reason':'최근 3분기 및 전년 동기 실적 확인 대기'}
    qs=sorted(quarters,key=lambda q:q['period'])[-3:]
    ids=[int(q['period'][:4])*4+int(q['period'][-1])-1 for q in qs]
    if ids[1]!=ids[0]+1 or ids[2]!=ids[1]+1:
        return {'growth':None,'fallen':None,'reason':'연속 분기 자료 확인 필요'}
    growth=all(q['revenue']>q['revenue_prev_year']>0 and q['operating_profit']>q['operating_profit_prev_year']>0 for q in qs)
    q=qs[-1];prev=q['operating_profit_prev_year'];op=q['operating_profit']
    good=op>0 and (prev<=0 or op>=prev*1.2)
    fields=['return120','drawdown52','average_amount20','price']
    if any(not isinstance(row.get(k),(int,float)) for k in fields) or row.get('risk_cleared') is not True:
        return {'growth':None,'fallen':None,'reason':'주가·거래대금·위험종목 확인 대기'}
    liquid=row['average_amount20']>=3e9 and row['price']>=1000
    return dict(growth=growth and liquid and row['return120']<=10,
                fallen=good and liquid and row['drawdown52']<=-25,reason='검증 자료 기준')


class SwingResearch:
    def __init__(self,data_dir):
        self.path=Path(data_dir)/'swing_research.json';self.lock=asyncio.Lock();self.error=''

    def snapshot(self):
        try: result=json.loads(self.path.read_text('utf-8'))
        except (OSError,ValueError): result={'asof':None,'industries':[],'total':0,'complete':False}
        return result|{'refreshing':self.lock.locked(),'error':self.error,
            'fundamentals':{'status':'not_connected','growth':[],'fallen':[],
                'message':'DART 분기 실적·업종 분류·위험종목 자료가 연결되지 않았습니다. 조건에 맞는 종목이 0개라는 뜻이 아닙니다.'}}

    async def refresh(self):
        if self.lock.locked(): raise HTTPException(409,'이미 자료를 갱신하고 있습니다.')
        async with self.lock:
            nr=NaverResearch();rows=[];complete=False;today=date.today();since=(today-timedelta(days=89)).isoformat()
            try:
                for page in range(1,31):
                    raw=await nr.json('/research/industry',{'page':page,'pageSize':100})
                    if not isinstance(raw,list): raise ValueError('리포트 목록 형식 확인 필요')
                    part=parse_list(raw,'산업');rows+=part
                    if not part or min(r.date for r in part)<since:
                        complete=True;break
                result=aggregate(rows,today,complete)
                if not result['total']: raise ValueError('최근 90일 산업 리포트 수신 결과가 없습니다.')
                tmp=self.path.with_suffix('.tmp');tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8');tmp.replace(self.path)
                self.error=''
            except Exception as e:
                self.error='산업 리포트 갱신 실패 · 이전 자료가 있다면 그대로 표시합니다.'
                raise HTTPException(502,self.error) from e
            finally: await nr.close()
        return self.snapshot()


def create_swing_router(data_dir):
    service=SwingResearch(data_dir);router=APIRouter()
    def local(request):
        if not request.client or request.client.host not in ('127.0.0.1','::1','testclient') or request.url.hostname not in ('127.0.0.1','localhost','::1','testserver'):
            raise HTTPException(403,'이 PC에서만 사용할 수 있습니다.')
        if request.headers.get('origin') and request.headers['origin'].rstrip('/')!=str(request.base_url).rstrip('/'):
            raise HTTPException(403,'요청 출처를 확인하세요.')
    @router.get('/api/swing-research')
    async def snapshot(request:Request):
        local(request);return service.snapshot()
    @router.post('/api/swing-research/refresh')
    async def refresh(request:Request):
        local(request);return await service.refresh()
    return router
