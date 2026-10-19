"""관제탑 읽기 전용 수집기. 기존 KiwoomRest의 호출 제한을 공유한다."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from app.fanout import fanout
from app.horizon import Bar, KST, RULES, VERSION, baseline, evaluate, now_kst, valid_bar
from app.kiwoom.parse import num
from app.models import Candle

log = logging.getLogger(__name__)


def parse_minutes(body):
    out = {}
    if 'stk_min_pole_chart_qry' not in body:
        raise ValueError('키움 분봉 응답 형식 확인 필요')
    for r in body['stk_min_pole_chart_qry']:
        stamp = str(r.get('cntr_tm',''))
        if len(stamp)!=14 or not stamp.isdigit() or not '090000' <= stamp[8:] <= '153000':
            continue
        try:
            datetime.strptime(stamp,'%Y%m%d%H%M%S')
        except ValueError:
            continue
        b = Bar(stamp,*(abs(num(r.get(k))) for k in ('open_pric','high_pric','low_pric','cur_prc','trde_qty')))
        if valid_bar(b):
            out[stamp]=b
    return [out[t] for t in sorted(out)]


class HorizonService:
    def __init__(self, settings, get_feed, get_state):
        self.s, self.get_feed, self.get_state = settings, get_feed, get_state
        self.path = settings.data_dir/'horizon.json'
        self.daily, self.names, self.detail, self.index, self.master = {}, {}, {}, {}, {}
        self.scan_at, self.updated_at, self.error = None, None, None
        self.scope_warning = None
        self.job = {'running':False,'done':0,'total':0,'current':'대기','errors':0}
        self.lock = asyncio.Lock()
        self.task = None
        self.manual = None
        self.last_scan_key = None
        self.loaded = False

    def load(self):
        if self.loaded:
            return
        self.loaded = True
        try:
            if self.path.exists():
                d=json.loads(self.path.read_text('utf-8'))
                self.names=d.get('names',{})
                self.daily={c:[Candle(*r) for r in rows] for c,rows in d.get('daily',{}).items()}
                self.detail=d.get('detail',{})
                self.index=d.get('index',{})
                self.master=d.get('master',{})
                self.scan_at=d.get('scanAt')
                # 어느 단계(날짜+장중/종가)까지 전 종목을 긁었는지. 재시작 때 같은 단계면 700종목을 다시 받지 않는다.
                self.last_scan_key=d.get('scanKey') or None
            else:
                p=self.s.data_dir/'candles_kiwoom_market.json'
                if p.exists():
                    d=json.loads(p.read_text('utf-8'))
                    self.names={c:[n,t] for c,n,t in d.get('universeList',[])}
                    self.daily={c:[Candle(*r) for r in rows] for c,rows in d.get('candles',{}).items()}
                    self.scan_at=d.get('savedAt')
        except (ValueError,TypeError,OSError):
            self.error='저장 자료를 읽지 못했습니다. 다시 수집해 주세요.'

    def save(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        d={'version':VERSION,'scanAt':self.scan_at,'scanKey':self.last_scan_key,'names':self.names,'index':self.index,'master':self.master,
           'daily':{c:[[x.date,x.open,x.high,x.low,x.close,x.volume,x.amount] for x in cs] for c,cs in self.daily.items()},
           'detail':self.detail}
        temp=self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(d,ensure_ascii=False),'utf-8')
        temp.replace(self.path)

    def rest(self):
        f=self.get_feed()
        # MockFeed와 KIS 값을 실제 키움 자료로 섞지 않는다.
        from app.feeds import KiwoomFeed
        return f.rest if isinstance(f,KiwoomFeed) else None

    def sessions(self):
        today=now_kst().strftime('%Y%m%d')
        return sorted(d for d in self.index if d<=today) or sorted({x.date for cs in self.daily.values() for x in cs if x.date<today})

    def candidates(self):
        sessions=self.sessions()
        recent=set(sessions[-5:])
        out=[]
        for code,cs in self.daily.items():
            hits=[i for i,c in enumerate(cs) if c.date in recent and baseline(cs,i)]
            # 종목당 가장 최근 기준봉. 최근 반복 여부는 evaluate에서 앞 10일로 판정.
            if hits:
                out.append((code,cs,hits[-1]))
        return sorted(out,key=lambda x:x[1][x[2]].amount,reverse=True)

    async def reference(self,rest):
        body,_=await rest.post('/api/dostk/chart','ka20006',{'inds_cd':'101','base_dt':now_kst().strftime('%Y%m%d')})
        rows=body.get('inds_dt_pole_qry')
        if not rows:
            raise ValueError('코스닥 거래일·상대강도 자료 없음')
        index={str(r.get('dt')):abs(num(r.get('cur_prc'))) for r in rows
               if len(str(r.get('dt','')))==8 and abs(num(r.get('cur_prc')))>0 and abs(num(r.get('trde_qty')))>0}
        if not index:
            raise ValueError('거래일 확인 불가')
        self.index=index

    async def metadata(self,rest):
        master={}
        for market in ('0','10'):
            nk=''
            seen=set()
            for _ in range(30):
                body,nk=await rest.post('/api/dostk/stkinfo','ka10099',{'mrkt_tp':market},nk)
                if 'list' not in body:
                    raise ValueError('종목 상태 목록 확인 불가')
                for r in body['list']:
                    code=str(r.get('code',''))
                    if len(code)==6 and code.isalnum():
                        master[code]={'state':str(r.get('state','')),'name':str(r.get('name',code)),
                                      'warning':str(r.get('orderWarning','')),'date':now_kst().strftime('%Y%m%d')}
                if not nk:
                    break
                if nk in seen:
                    raise ValueError('종목 목록 연속조회 반복')
                seen.add(nk)
            else:
                raise ValueError('종목 목록 연속조회 한도')
        self.master=master

    async def scan(self,rest):
        from app.collectors.naver_daily import NaverDaily
        # 기존 일봉 범위 + 현재 테마 + 당일 대금100억 이상. 전체 시장이라고 표시하지 않는다.
        uni=dict(self.names)
        for t in self.get_state().themes:
            for s in t.stocks:
                uni[s.code]=[s.name,t.name]
        nd=NaverDaily()
        try:
            for s in await nd.market_list(min_amount_eok=100):
                uni.setdefault(s.code,[s.name,s.market])
            self.scope_warning=None
        except Exception:
            # 종목 목록 소스가 잠깐 끊겨도 이미 관찰하던 종목의 오전 감시는 계속한다.
            self.scope_warning='당일 100억 이상 종목 목록 조회 실패 · 기존 범위만 수집'
        finally:
            await nd.close()
        if not uni:
            raise ValueError('관제탑 검색 범위가 비었습니다')
        self.job.update(total=len(uni),done=0,current='기준봉 찾는 중')
        done=[0]

        async def one(item):
            # 종목마다 하나씩 기다리면 '호출 간격 + 왕복시간' 이 매번 더해진다. 몇 개를 겹쳐 보낸다.
            code,label=item
            ok=False
            try:
                cs=await rest.daily_candles(code,280)
                if not cs:
                    raise ValueError('일봉 없음')
                self.daily[code]=cs
                self.names[code]=label
                ok=True
            except Exception:
                self.job['errors']+=1
            done[0]+=1
            self.job.update(done=done[0],current=label[0])
            return ok

        success=sum(1 for ok in await fanout(list(uni.items()),one) if ok is True)
        if not success:
            raise ValueError('일봉을 받지 못했습니다. 기존 자료만 표시합니다.')
        self.scan_at=now_kst().isoformat(timespec='seconds')

    async def recent_daily(self,rest,code,keep=280,recent=10):
        """최근 일봉만 받아 쌓아 둔 일봉에 덮는다 (280일을 1분마다 다시 받지 않으려고).

        겹치는 날(오늘 봉 제외)의 종가가 저장된 것과 다르면 권리 조정(수정주가)이 된 것이므로 전체를 다시 받는다.
        쌓아 둔 게 없으면 받은 것만 돌려주고, 다음 전 종목 수집이 280일을 채운다."""
        fresh=await rest.daily_candles(code,recent)
        have=self.daily.get(code) or []
        if not fresh or not have:
            return fresh
        old={c.date:c for c in have}
        if any(c.date in old and old[c.date].close!=c.close for c in fresh[:-1]):
            return await rest.daily_candles(code,keep)
        merged=old|{c.date:c for c in fresh}
        return [merged[d] for d in sorted(merged)][-keep:]

    async def minutes(self,rest,code,since):
        nk=''
        seen=set()
        out={}
        for _ in range(12):
            body,nk=await rest.post('/api/dostk/chart','ka10080',{'stk_cd':code,'tic_scope':'15','upd_stkpc_tp':'1'},nk)
            chunk=parse_minutes(body)
            out.update({b.stamp:asdict(b) for b in chunk})
            if (chunk and chunk[0].stamp[:8]<since) or not nk:
                break
            if nk in seen:
                raise ValueError('분봉 연속조회 반복')
            seen.add(nk)
        else:
            raise ValueError('분봉 조회 범위 부족')
        return [out[t] for t in sorted(out) if t[:8]>=since]

    async def refresh(self,full=False):
        if self.lock.locked():
            return
        async with self.lock:
            self.load()
            self.job.update(running=True,errors=0,done=0,total=0,current='연결 확인')
            try:
                rest=self.rest()
                if rest is None:
                    raise ValueError('키움 시세 연결 대기 · 실제 자료만 판정합니다')
                await self.reference(rest)
                today=now_kst().strftime('%Y%m%d')
                if full or not self.master or any(x.get('date')!=today for x in self.master.values()):
                    await self.metadata(rest)
                if full:
                    await self.scan(rest)
                candidates=self.candidates()
                self.job.update(total=len(candidates),done=0)
                done=[0]

                async def one(item):
                    code,cs,i=item
                    try:
                        basic,_=await rest.post('/api/dostk/stkinfo','ka10001',{'stk_cd':code})
                        # D+1 당일 고가/종가를 놓치지 않도록 일봉도 갱신. 지난 일봉은 바뀌지 않으므로 최근 것만 받는다.
                        daily=await self.recent_daily(rest,code)
                        if not daily:
                            raise ValueError('종목 일봉 없음')
                        bars=await self.minutes(rest,code,cs[i].date)
                        old=self.detail.get(code,{})
                        # 과거 오전 이탈 기록은 통신 재연결/부분 응답 뒤에도 보존.
                        merged={b['stamp']:b for b in old.get('bars',[]) if b['stamp'][:8]>=cs[i].date}
                        merged.update({b['stamp']:b for b in bars})
                        self.daily[code]=daily
                        self.detail[code]={'cap':abs(num(basic.get('mac'))) or None,
                                           'price':abs(num(basic.get('cur_prc'))) or None,
                                           'change':num(basic.get('flu_rt')) if basic.get('flu_rt') is not None else None,
                                           'bars':[merged[t] for t in sorted(merged)],'fetchedAt':now_kst().isoformat(timespec='seconds')}
                    except Exception:
                        self.job['errors']+=1
                        # 이전 수신 시각을 남긴다. 실패를 최신 자료로 포장하지 않는다.
                    done[0]+=1
                    self.job.update(done=done[0],current=self.names.get(code,[code])[0])

                await fanout(candidates,one)      # 후보마다 ka10001·일봉·분봉 세 묶음을 겹쳐 받는다
                active={c for c,_,_ in self.candidates()}
                self.detail={c:v for c,v in self.detail.items() if c in active}
                self.updated_at=now_kst().isoformat(timespec='seconds')
                self.error=' · '.join(x for x in [self.scope_warning,
                    f"{self.job['errors']}종목 조회 실패 · 해당 종목 수신 시각 확인" if self.job['errors'] else None] if x) or None
                self.save()
            except Exception as e:
                self.error=str(e)
                log.warning('관제탑 수집: %s',e)
            finally:
                self.job.update(running=False,current='대기')

    async def loop(self):
        self.load()
        while True:
            now=now_kst()
            key=now.strftime('%Y%m%d')+('close' if now.strftime('%H%M')>='1540' else 'day')
            full=key!=self.last_scan_key
            if full or (now.weekday()<5 and '0900'<=now.strftime('%H%M')<='1540'):
                await self.refresh(full=full)
                if not self.error or (self.scan_at and self.scan_at[:10]==now.date().isoformat()):
                    self.last_scan_key=key
            await asyncio.sleep(60 if not self.error else 180)

    def start(self):
        if self.task is None or self.task.done():
            self.task=asyncio.create_task(self.loop(),name='horizon')

    async def stop(self):
        tasks=[t for t in (self.task,self.manual) if t and not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks,return_exceptions=True)

    def snapshot(self,at=None):
        self.load()
        at=at or now_kst()
        sessions=self.sessions()
        state=self.get_state()
        rows=[]
        for code,cs,i in self.candidates():
            d=self.detail.get(code,{})
            info=self.master.get(code,{})
            st=info.get('state','')
            restricted=any(k in st for k in ('거래정지','관리종목','불성실','정리매매')) if st and info.get('date')==at.strftime('%Y%m%d') else None
            row=evaluate(code,*self.names.get(code,[code,'미분류']),cs,i,sessions,self.index,[Bar(**b) for b in d.get('bars',[])],
                         at=at,cap=d.get('cap'),restricted=restricted,fetched_at=d.get('fetchedAt') if self.index else None)
            row.update(price=d.get('price'),change=d.get('change'),stockState=st)
            t=state.theme_of.get(code)
            row['news']={'title':t.news,'url':t.news_url,'at':t.news_at,'scope':'테마 관련 뉴스'} if t else None
            rows.append(row)
        order={'ready':0,'preview':1,'waiting':2,'incomplete':3,'watch':4,'baseline':5,'review':6,'excluded':7}
        rows.sort(key=lambda r:(order[r['status']],-r['base']['amountEok']))
        return {'version':VERSION,'serverAt':at.isoformat(timespec='seconds'),'updatedAt':self.updated_at,'scanAt':self.scan_at,
                'asof':sessions[-1] if sessions else None,'calendarConfirmed':bool(self.index),
                'scope':f'기존 일봉·테마·당일 100억 이상 중 {len(self.daily)}종목',
                'job':dict(self.job),'error':self.error,'rows':rows,
                'rules':[{'category':c,'title':t,'text':v} for c,t,v in RULES],
                'ordersEnabled':False,'autoBuy':False,'exit':{'profit':15,'loss':-10,'holdSessions':3}}


def create_horizon_router(service):
    router=APIRouter()

    @router.get('/api/horizon')
    async def snapshot():
        # 페이지가 열린 동안 실행. 앱 시작 때 대규모 수집으로 기존 시세를 지연시키지 않는다.
        service.start()
        return service.snapshot()

    @router.post('/api/horizon/refresh')
    async def refresh():
        if service.job['running'] or (service.manual and not service.manual.done()):
            raise HTTPException(409,'이미 수집 중입니다')
        service.manual=asyncio.create_task(service.refresh(full=True),name='horizon-refresh')
        return {'ok':True}

    return router
