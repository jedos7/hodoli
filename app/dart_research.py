"""DART historical single-quarter screening; strictly read-only market research."""
import asyncio, io, json, re, zipfile, logging
from datetime import date, datetime, timedelta
from pathlib import Path
from collections import defaultdict
from xml.etree import ElementTree as ET
import httpx
from fastapi import APIRouter, Request, HTTPException
from app.dart_credentials import load_key
from app.collectors.naver_daily import NaverDaily

class Redact(logging.Filter):
    def filter(self,r):
        r.msg=re.sub(r'(crtfc_key=)[^&\s\"\']+',r'\1[REDACTED]',r.getMessage());r.args=();return True
for logger in ('httpx','httpcore'):logging.getLogger(logger).addFilter(Redact())

def number(s):
    s=str(s).replace(',','').strip()
    return int(s) if re.fullmatch(r'-?\d+',s) else None

def latest_period(today):
    for year in range(today.year,today.year-2,-1):
        for q,month,day in [(4,3,31),(3,11,15),(2,8,15),(1,5,15)]:
            deadline=date(year+1,month,day) if q==4 else date(year,month,day)
            if deadline<=today:return year,q

def periods(year,q):
    return [divmod(year*4+q-1-offset,4) for offset in (2,1,0)]

def select_accounts(rows):
    result={}
    for r in rows:
        name=r.get('account_nm','').replace(' ','')
        field='revenue' if name in ('매출액','수익(매출액)','영업수익') else 'operating_profit' if name in ('영업이익','영업이익(손실)') else None
        if not field or r.get('sj_div')!='IS' or r.get('currency')!='KRW':continue
        year=str(r.get('bsns_year',''))
        end={'11013':'03.31','11012':'06.30','11014':'09.30','11011':'12.31'}.get(r.get('reprt_code'))
        period=r.get('thstrm_dt','').replace(' ','')
        if period!=f'{year}.01.01~{year}.{end}':continue
        key=(r['stock_code'],r['fs_div'],field)
        # Ambiguous duplicate accounts are not summed or silently preferred.
        values=(number(r.get('thstrm_amount')),number(r.get('frmtrm_amount')),number(r.get('thstrm_add_amount')),number(r.get('frmtrm_add_amount')))
        if key in result and result[key]!=values:result[key]=(None,)*4
        else:result[key]=values
    return result

def quarter(records,code,basis,year,q):
    out={'period':f'{year}Q{q}'}
    for field in ('revenue','operating_profit'):
        current=records.get((year,q),{}).get((code,basis,field))
        if current is None:return None
        a,b=current[:2]
        if q==4:
            third=records.get((year,3),{}).get((code,basis,field))
            if not third or a is None or b is None or third[2] is None or third[3] is None:return None
            a-=third[2];b-=third[3]
        if a is None or b is None:return None
        out[field]=a;out[field+'_prev_year']=b
    return out

def evaluate(records,code,wanted):
    # Never mix consolidated and separate statements across periods.
    for basis in ('CFS','OFS'):
        qs=[quarter(records,code,basis,y,q) for y,q in wanted]
        if all(qs):
            growth=all(q['revenue_prev_year']>0 and q['revenue']>q['revenue_prev_year'] and q['operating_profit']>0 and q['operating_profit']>q['operating_profit_prev_year'] for q in qs)
            last=qs[-1];op=last['operating_profit'];prior=last['operating_profit_prev_year']
            return dict(basis=basis,quarters=qs,growth=growth,good=op>0 and (prior<=0 or op>=prior*1.2))
    return None

class DartResearch:
    def __init__(self,directory):
        self.directory=Path(directory);self.path=self.directory/'dart_research.json';self.task=None;self.progress='';self.error=''
    def snapshot(self):
        try:result=json.loads(self.path.read_text('utf-8'))
        except (OSError,ValueError):result=dict(asof=None,rows=[],growth=[],fallen=[],unknown=0)
        return result|dict(connected=(self.directory/'dart_credential.json').exists(),busy=bool(self.task and not self.task.done()),progress=self.progress,error=self.error)
    def start(self):
        if not self.task or self.task.done():self.task=asyncio.create_task(self.refresh())
        return self.snapshot()
    async def refresh(self):
        api=NaverDaily();self.error='';self.progress='DART 법인 목록 확인 중'
        try:
            key=load_key(self.directory)
            if not key:raise ValueError('key')
            async with httpx.AsyncClient(timeout=45) as client:
                r=await client.get('https://opendart.fss.or.kr/api/corpCode.xml',params={'crtfc_key':key});r.raise_for_status()
                with zipfile.ZipFile(io.BytesIO(r.content)) as z:xml=ET.fromstring(z.read('CORPCODE.xml'))
                mapping={x.findtext('stock_code').strip():x.findtext('corp_code') for x in xml.findall('list') if (x.findtext('stock_code') or '').strip()}
                # Current listed/trading common shares, not a survivorship-free historical backtest.
                stocks=await api.market_list(min_amount_eok=0);self.progress='가격 조건 확인 중'
                candidates=[];failed=[];sem=asyncio.Semaphore(3);done=0
                async def prices(s):
                    nonlocal done
                    if s.code not in mapping or s.price<1000:return
                    async with sem:
                        try:
                            cs=await api.candles(s.code,330)
                            # Ignore today's incomplete daily bar.
                            cs=[c for c in cs if c.date<date.today().strftime('%Y%m%d')]
                            if len(cs)<253:return
                            amount=sum(c.amount for c in cs[-20:])/20
                            if amount<3e9 or cs[-1].close<1000:return
                            candidates.append(dict(code=s.code,name=s.name,market=s.market,price=cs[-1].close,price_date=cs[-1].date,
                                average_amount20=amount,return120=(cs[-1].close/cs[-121].close-1)*100,
                                drawdown52=(cs[-1].close/max(c.high for c in cs[-252:])-1)*100,risk='위험종목 별도 확인 필요'))
                        except Exception:failed.append(s.code)
                        finally:done+=1;self.progress=f'가격 확인 {done}/{len(stocks)} · 거래대금 후보 {len(candidates)}'
                await asyncio.gather(*(prices(s) for s in stocks))
                if not candidates:raise ValueError('prices')
                profile_done=0
                async def profile(row):
                    nonlocal profile_done
                    async with sem:
                        try:
                            response=await client.get('https://opendart.fss.or.kr/api/company.json',params={'crtfc_key':key,'corp_code':mapping[row['code']]})
                            response.raise_for_status();body=response.json()
                            if body.get('status')=='000':
                                row['fiscal_month']=body.get('acc_mt');row['industry']=body.get('induty_code') or '미분류'
                        except Exception:pass
                        profile_done+=1;self.progress=f'결산월·업종 확인 {profile_done}/{len(candidates)}';await asyncio.sleep(.15)
                await asyncio.gather(*(profile(r) for r in candidates))
                unsupported=sum(str(r.get('fiscal_month','')).zfill(2)!='12' for r in candidates)
                candidates=[r for r in candidates if str(r.get('fiscal_month','')).zfill(2)=='12']
                if not candidates:raise ValueError('profiles')
                year,q=latest_period(date.today());wanted=[(y,i+1) for y,i in periods(year,q)]
                requests=set(wanted)|{(y,3) for y,q in wanted if q==4};records={};reports={1:'11013',2:'11012',3:'11014',4:'11011'}
                for y,q in sorted(requests):
                    rows=[]
                    for start in range(0,len(candidates),100):
                        self.progress=f'DART {y}년 {q}분기 · {min(start+100,len(candidates))}/{len(candidates)}종목'
                        response=await client.get('https://opendart.fss.or.kr/api/fnlttMultiAcnt.json',params=dict(crtfc_key=key,corp_code=','.join(mapping[x['code']] for x in candidates[start:start+100]),bsns_year=y,reprt_code=reports[q]))
                        response.raise_for_status();body=response.json()
                        if body.get('status') not in ('000','013'):raise ValueError('dart')
                        rows.extend(body.get('list',[]));await asyncio.sleep(.2)
                    records[y,q]=select_accounts(rows)
                result=[];unknown=[]
                for row in candidates:
                    evaluated=evaluate(records,row['code'],wanted)
                    if evaluated is None:unknown.append(row['code']);continue
                    result.append(row|evaluated)
                if not result:raise ValueError('empty')
                output=dict(asof=datetime.now().isoformat(timespec='seconds'),periods=[f'{y}Q{q}' for y,q in wanted],universe=len(stocks),price_candidates=len(candidates),unknown=len(unknown),unsupported_fiscal=unsupported,failed_prices=len(failed),rows=result,
                    growth=[r for r in result if r['growth'] and r['return120']<=10],fallen=[r for r in result if r['good'] and r['drawdown52']<=-25],
                    note='DART 연결 우선·없으면 별도, 동일 재무기준 3분기. 4분기는 연간−3분기 누적. 전년 동기는 같은 보고서 비교값. 12월 결산 기업 기준. 거래대금은 수정종가×거래량 근사. 거래정지는 목록에서 제외하나 관리·투자경고 등 위험 분류는 별도 확인이 필요한 후보입니다.')
                tmp=self.path.with_suffix('.tmp');tmp.write_text(json.dumps(output,ensure_ascii=False),'utf-8');tmp.replace(self.path);self.progress=f'조회 완료 · 재무 확인 {len(result)}종목'
        except Exception:self.error='실적 수집 실패 · 인증·조회 한도·자료 연결 상태를 확인해주세요. 이전 결과는 유지합니다.'
        finally:await api.close()

def create_dart_router(directory):
    service=DartResearch(directory);router=APIRouter()
    def local(r):
        if not r.client or r.client.host not in ('127.0.0.1','::1','testclient') or r.url.hostname not in ('localhost','127.0.0.1','::1','testserver'):raise HTTPException(403,'로컬 조회만 가능합니다')
        if r.headers.get('origin') and r.headers['origin'].rstrip('/')!=str(r.base_url).rstrip('/'):raise HTTPException(403,'요청 출처 확인 필요')
    @router.get('/api/dart-research')
    async def read(request:Request):local(request);return service.snapshot()
    @router.post('/api/dart-research/refresh')
    async def refresh(request:Request):local(request);return service.start()
    return router
