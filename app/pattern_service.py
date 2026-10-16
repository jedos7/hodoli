"""Read-only daily signal API; no account or order dependencies."""
import asyncio
import time
from datetime import datetime, timezone, timedelta
KST = timezone(timedelta(hours=9))
from fastapi import APIRouter, HTTPException, Query
from app.collectors.naver_daily import NaverDaily
from app.screener.pattern_signals import analyze, Rules


def create_pattern_router(get_state):
    router=APIRouter(prefix='/api/pattern-signals')
    cache={}
    lock=asyncio.Lock()

    @router.get('/{theme_id}')
    async def signals(theme_id: str, spike_ratio: float=Query(2,ge=1,le=10),
                      base_width: float=Query(.12,ge=.01,le=.5), near112: float=Query(.03,ge=.005,le=.2)):
        state = get_state()
        theme=next((t for t in state.themes if t.id==theme_id),None)
        if theme is None: raise HTTPException(404,'테마 없음')
        now=datetime.now(KST)
        # Conservatively exclude today's candle, including after hours; avoids partial daily bars.
        cutoff=now.strftime('%Y%m%d')
        rules=Rules(spike_ratio=spike_ratio,base_width=base_width,near112=near112)
        results=[]
        async with lock:
            source=NaverDaily()
            try:
                for stock in list(theme.stocks):
                    key=(stock.code,cutoff)
                    hit=cache.get(key)
                    try:
                        if hit is None or time.monotonic()-hit[0]>900:
                            bars=await source.candles(stock.code,count=560)
                            bars=[b for b in bars if b.date<cutoff]
                            hit=(time.monotonic(),bars,now.isoformat())
                            cache[key]=hit
                        result=analyze(hit[1],rules)
                        result['fetched_at']=hit[2]
                    except Exception:
                        result=dict(stage='조회 실패',rank=-1,signal256='조회 실패',asof=None,conditions={})
                    results.append(dict(code=stock.code,name=stock.name,**result))
                for key in list(cache):
                    if key[1]!=cutoff: del cache[key]
            finally:
                await source.close()
        return dict(theme_id=theme_id,source='네이버 수정주가 일봉',policy='당일 봉 제외',rows=results)
    return router
