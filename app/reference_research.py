"""Read-only chart research. No trading or account imports."""
import asyncio
import json
import math
import httpx
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from dataclasses import asdict
from fastapi import APIRouter, HTTPException, Request
from app.collectors.naver_daily import NaverDaily


def market_rows(candles):
    daily=defaultdict(list)
    for code, bars in candles.items():
        bars=sorted(bars,key=lambda b:b[0])
        for i,b in enumerate(bars):
            if len(b)<6 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in b[1:6]) or b[4]<=0 or b[5]<0: continue
            prev=bars[i-1][4] if i else None
            daily[b[0]].append(dict(amount=b[4]*b[5],rise=int(b[4]>prev) if prev else None,
                surge=int(b[4]/prev>=1.29) if prev else None,
                high=int(b[2]>=max(x[2] for x in bars[i-252:i])) if i>=252 else None))
    result=[]
    for day, rows in sorted(daily.items()):
        amount=sum(r['amount'] for r in rows); compared=[r for r in rows if r['rise'] is not None]
        highs=[r for r in rows if r['high'] is not None]
        result.append(dict(date=day,count=len(rows),amount=amount/1e12,
            rising=100*sum(r['rise'] for r in compared)/len(compared) if compared else None,
            surge=sum(r['surge'] for r in compared) if compared else None,
            new_high=sum(r['high'] for r in highs) if highs else None,new_high_coverage=len(highs),
            concentration=100*sum(sorted((r['amount'] for r in rows),reverse=True)[:math.ceil(len(rows)*.2)])/amount if amount else None,
            deposits=None,credit=None,credit_ratio=None,forced_sales=None,kospi=None))
    for i,r in enumerate(result):
        avg20=sum(x['amount'] for x in result[i-19:i+1])/20 if i>=19 else None
        avg60=sum(x['amount'] for x in result[i-59:i+1])/60 if i>=59 else None
        r['liquidity']=avg20/avg60 if avg60 else None
        r['vs20']=(r['amount']/avg20-1)*100 if avg20 else None
        r['regime']='자료 부족' if r['liquidity'] is None else ('유동성 확장' if r['liquidity']>=1.1 else '유동성 축소' if r['liquidity']<.9 else '유동성 보통')
    return result


class ReferenceResearch:
    def __init__(self,directory):
        self.directory=Path(directory);self.path=self.directory/'reference_market.json'
        self.task=None;self.progress='';self.error='';self.cached=None;self.version=None
    def snapshot(self):
        source=self.path if self.path.exists() else self.directory/'candles_naver_all.json'
        try:
            version=(str(source),source.stat().st_mtime)
            if self.version!=version:
                body=json.loads(source.read_text('utf-8'))
                self.cached=dict(asof=body.get('savedAt'),rows=market_rows(body.get('candles',{})),
                    stocks=body.get('stocks',[]),failed=body.get('failed',[]))
                for row in self.cached['rows']:
                    funds=body.get('funds',{}).get(row['date'],{})
                    row.update(funds)
                self.version=version
        except (OSError,ValueError): self.cached=dict(asof=None,rows=[],stocks=[],failed=[])
        return self.cached|dict(busy=bool(self.task and not self.task.done()),progress=self.progress,error=self.error,
            source='네이버 수정 일봉 · 현재 수집 가능한 보통주 표본',
            note='거래대금은 종가×거래량 근사치입니다. 상장폐지·거래정지 등 제외와 날짜별 종목 수 차이가 있어 시장 전체 공식 통계와 다릅니다. 29%↑는 상한가 수가 아닙니다. 유동성은 20일 평균/60일 평균, 확장≥1.1·축소<0.9 자체 기준입니다.',
            missing='예탁금·신용은 네이버 증시자금동향의 발표일 기준입니다. 당일 미발표 자료는 빈칸입니다. 반대매매·공식 지수는 연결 대기이며 빈 값을 0으로 표시하지 않습니다.')
    def start(self):
        if self.task and not self.task.done():return self.snapshot()
        self.task=asyncio.create_task(self.refresh());return self.snapshot()
    async def refresh(self):
        api=NaverDaily();self.error='';self.progress='종목 목록 확인 중'
        try:
            stocks=await api.market_list(min_amount_eok=0)
            if not stocks:raise ValueError('empty')
            bars={};failed=[];sem=asyncio.Semaphore(3);done=0
            async def one(stock):
                nonlocal done
                async with sem:
                    try:
                        cs=await api.candles(stock.code,330)
                        if not cs:raise ValueError('empty')
                        bars[stock.code]=[[c.date,c.open,c.high,c.low,c.close,c.volume] for c in cs]
                    except Exception:failed.append(stock.code)
                    done+=1;self.progress=f'일봉 수집 {done:,}/{len(stocks):,}종목 · 실패 {len(failed)}'
            await asyncio.gather(*(one(s) for s in stocks))
            if len(bars)<len(stocks)*.9:raise ValueError('coverage')
            funds={}
            try:
                async with httpx.AsyncClient(timeout=20) as c:
                    for start in range(0,400,100):
                        r=await c.get('https://stock.naver.com/api/domestic/market/trendDeposit',params={'startIdx':start,'pageSize':100})
                        r.raise_for_status();entries=r.json().get('content',[])
                        for item in entries:
                            dep=float(item['customerDeposit'].replace(',',''))/10000
                            credit=float(item['creditLoan'].replace(',',''))/10000
                            if dep>0 and credit>=0:funds[item['bizdate']]=dict(deposits=dep,credit=credit,credit_ratio=credit/dep*100)
                        if len(entries)<100:break
            except Exception:pass
            body=dict(savedAt=datetime.now().isoformat(timespec='seconds'),candles=bars,stocks=[asdict(s) for s in stocks],failed=failed,funds=funds)
            tmp=self.path.with_suffix('.tmp');tmp.write_text(json.dumps(body,ensure_ascii=False),'utf-8');tmp.replace(self.path)
            self.progress=f'{len(bars):,}종목 갱신 완료';self.version=None
        except Exception:self.error='일봉 수집 실패 · 기존 자료를 유지합니다.'
        finally:await api.close()


def create_reference_router(directory):
    service=ReferenceResearch(directory);router=APIRouter()
    def local(r):
        if not r.client or r.client.host not in ('127.0.0.1','::1','testclient') or r.url.hostname not in ('localhost','127.0.0.1','::1','testserver'):raise HTTPException(403,'로컬 조회만 가능합니다')
        if r.headers.get('origin') and r.headers['origin'].rstrip('/')!=str(r.base_url).rstrip('/'):raise HTTPException(403,'출처 확인 필요')
    @router.get('/api/reference-market')
    async def read(request:Request):local(request);return await asyncio.to_thread(service.snapshot)
    @router.post('/api/reference-market/refresh')
    async def refresh(request:Request):local(request);return service.start()
    return router
