import asyncio
from datetime import timedelta
from tests.test_horizon_plan import v2, arm, drain

async def profit_filled(r):
 p=await arm(r);r.e.quote('005930',11500,r.now[0],'KRX');await drain(r)
 r.b.response={'filled':7,'remaining':0,'rejected':False};r.b.row.update(quantity=3,available=3)
 await r.e.reconcile();return p

def test_trailing_boundary_and_no_repeat(v2):
 async def run():
  p=await profit_filled(v2)
  v2.e.quote('005930',12000,v2.now[0],'KRX');await drain(v2)
  assert p['trailing_high']==12000
  v2.e.quote('005930',11641,v2.now[0],'KRX');await drain(v2);assert len(v2.b.calls)==1
  for _ in range(3):v2.e.quote('005930',11640,v2.now[0],'KRX');await drain(v2)
  assert v2.b.calls==[('005930',7,11500),('005930',3)]
  assert v2.e.orders[-1]['reason']=='TRAILING_STOP'
 asyncio.run(run())

def test_no_trail_before_profit_fill(v2):
 async def run():
  p=await arm(v2);v2.e.quote('005930',11400,v2.now[0],'KRX');await drain(v2)
  v2.e.quote('005930',11000,v2.now[0],'KRX');await drain(v2)
  assert not v2.b.calls and not p['trailing_started_at']
 asyncio.run(run())

def test_stale_quote_cannot_raise_high(v2):
 async def run():
  p=await profit_filled(v2)
  v2.e.quote('005930',20000,v2.now[0]-timedelta(seconds=11),'KRX');await drain(v2)
  assert p['trailing_high']==11500
 asyncio.run(run())

def test_partial_profit_not_armed(v2):
 async def run():
  p=await arm(v2);v2.e.quote('005930',11500,v2.now[0],'KRX');await drain(v2)
  v2.b.response={'filled':3,'remaining':4,'rejected':False};await v2.e.reconcile()
  assert p['status']=='ordered' and not p['trailing_started_at']
 asyncio.run(run())

from tests.test_autoexit import rig
