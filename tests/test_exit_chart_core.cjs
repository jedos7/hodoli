const assert=require('node:assert/strict'),core=require('../static/exit-chart-core.js');
const candle=(stamp,change={})=>({stamp,open:83800,high:84000,low:83000,close:83900,volume:100,...change});
const bars=core.bars([candle('20260923113000'),candle('20260923112900'),candle('20260923113000',{close:83850}),
  candle('invalid'),candle('20260923112800',{high:80000}),candle('20260923112700',{close:NaN})]);
assert.equal(bars.length,2);assert.equal(bars[0].stamp,'20260923112900');assert.equal(bars[1].close,83850);
const p={code:'126340',name:'비나텍',quantity:340,average:81217,profit_protection:{price:'83800'},take_profit:{price:'93400'},stop_loss:{price:'73000'},status:'prepared'};
assert.deepEqual(core.lines(core.fromPlan(p)).map(l=>[l.kind,l.price]),[['average',81217],['protection',83800],['profit',93400],['loss',73000]]);
assert(!core.lines(core.fromPlan({...p,take_profit_done:true})).some(l=>l.kind==='profit'));
const inputs={pp:{enabled:true,price:83800},tp:{enabled:false,pct:15},sl:{enabled:true,pct:10}};
const draft=core.fromDraft(p,inputs);assert.equal(draft.source,'draft');assert.equal(draft.profit,null);assert.equal(draft.loss,81217*.9);
assert.equal(core.fromPlan(p).source,'plan');assert.equal(p.status,'prepared');
assert.deepEqual(core.windowRange(600,100,0),{start:500,end:600});
assert.deepEqual(core.windowRange(600,100,50),{start:450,end:550});
assert.deepEqual(core.windowRange(2,100,50),{start:0,end:2});
const range=core.extent(bars,core.lines(core.fromPlan(p)));assert(range.low<73000 && range.high>93400);
console.log('PASS: real OHLC validation, sorting, price lines, completed TP, draft/plan separation and pan/zoom range');
const tickets={...p,conditions:[{kind:'protection',price:'83800',comparison:'lt',enabled:false,state:'paused'},
  {kind:'profit',price:'95200',comparison:'gte',enabled:true,state:'watching'},
  {kind:'profit',price:'97000',comparison:'gte',enabled:true,state:'watching'},
  {kind:'loss',price:'81000',comparison:'lte',enabled:false,state:'done'}]};
const levels=core.lines(core.fromPlan(tickets));
assert.equal(levels.length,4);assert.match(levels[1].label,/미만.*꺼짐/);assert.deepEqual(levels.slice(2).map(l=>l.price),[95200,97000]);
