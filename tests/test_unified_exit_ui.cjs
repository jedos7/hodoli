// Offline fake DOM/API. No live account calls, orders or sockets.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const flush=async()=>{for(let i=0;i<8;i++)await new Promise(r=>setImmediate(r));};
function harness(){
  const elements=new Map(),requests=[],timers=[],events={},charts=[];
  const el=id=>{if(!elements.has(id))elements.set(id,{id,value:'',checked:false,disabled:false,hidden:false,open:false,textContent:'',innerHTML:'',className:'',dataset:{},handlers:{},setAttribute(){},replaceChildren(){this.innerHTML='';},showModal(){this.open=true;},close(){this.open=false;},addEventListener(n,f){(this.handlers[n]??=[]).push(f);}});return elements.get(id);};
  const emit=async(id,event,args={})=>{for(const f of el(id).handlers[event]||[])await f({target:el(id),preventDefault(){},...args});await flush();};
  const modes=['custom','horizon'].map(m=>{const e=el(m);e.dataset.exitMode=m;return e;});
  const p={code:'005930',name:'가짜 <종목>',quantity:10,available:10,average:10000,bought_on:'2026-09-21'};
  let state={environment:'paper',token:'FAKE',enabled:false,connected:true,market:'3',error:'',asof:new Date().toISOString(),orders:[],plans:[]};
  const window={CustomExitUI:{selected:()=>el('code').value===p.code?p:null,clearReview(){},refreshChart(){},isBusy:()=>false},ExitChart:{setMode:m=>charts.push(m),setHorizon:p=>charts.push(p)},addEventListener:(n,f)=>events[n]=f};
  const context={window,document:{getElementById:el,hidden:false,querySelectorAll:s=>s==='[data-exit-mode]'?modes:[],addEventListener:(n,f)=>events[n]=f},history:{replaceState(){}},location:{hash:''},Date,AbortSignal,console,setInterval:f=>timers.push(f),fetch:async(url,options={})=>{
    const body=options.body?JSON.parse(options.body):undefined;requests.push({url,body});let data=state;
    if(url==='/api/autosell')data={positions:[p]};
    if(url.endsWith('/prepare')){data={...p,...body,id:'HZ1',environment:'paper',status:'prepared',prepared_at:new Date().toISOString()};state.plans=[data];}
    if(url.endsWith('/arm')){state.plans[0].status='armed';state.enabled=true;}
    if(url.endsWith('/pause')){state.plans.forEach(x=>x.status='paused');state.enabled=false;}
    return {ok:true,json:async()=>JSON.parse(JSON.stringify(data))};
  }};
  el('hzAfterHours').value='off';
  vm.runInNewContext(fs.readFileSync('static/unified-exit.js','utf8'),context);
  return {el,emit,requests,window,charts,state,events,timers};
}
(async()=>{
  const r=harness();await flush();assert(r.requests.every(x=>x.body===undefined));
  await r.emit('horizon','click');assert(r.el('customModePanel').hidden);assert(!r.el('horizonModePanel').hidden);assert(r.requests.every(x=>x.body===undefined));
  await r.emit('hzPrepareForm','submit');assert.match(r.el('hzError').textContent,/종목/);assert(!r.requests.some(x=>x.url.endsWith('/prepare')));
  r.el('code').value='005930';await r.emit('code','change');assert.equal(r.el('hzBoughtOn').value,'2026-09-21');
  r.window.ExitWorkspace.customStatus({plans:[{code:'005930',status:'armed'}],orders:[]});assert.equal(r.el('modeConflict').hidden,false);assert.match(r.el('modeConflict').textContent,/직접 설정/);
  r.window.ExitWorkspace.customStatus({plans:[],orders:[]});
  await r.emit('hzPrepareForm','submit');assert(r.el('hzReviewDialog').open);assert.match(r.el('hzReview').innerHTML,/&lt;종목&gt;/);
  assert.deepEqual(r.requests.find(x=>x.url.endsWith('/prepare')).body,{code:'005930',bought_on:'2026-09-21',after_hours:'off',retry_unfilled:false,empty_book:'wait'});
  await r.emit('hzArm','click');assert(!r.requests.some(x=>x.url.endsWith('/arm')));
  r.el('hzAck').checked=true;await r.emit('hzAck','change');await r.emit('custom','click');assert(!r.el('hzReviewDialog').open);
  await r.emit('hzArm','click');assert(!r.requests.some(x=>x.url.endsWith('/arm')));
  await r.emit('horizon','click');await r.emit('hzPrepareForm','submit');r.el('hzAck').checked=true;await r.emit('hzArm','click');
  assert.equal(r.requests.filter(x=>x.url.endsWith('/arm')).length,1);assert.deepEqual(r.requests.find(x=>x.url.endsWith('/arm')).body,{plan_id:'HZ1',environment:'paper',market_order:true});
  await r.emit('hzStopAll','click');assert.deepEqual(r.requests.find(x=>x.url.endsWith('/pause')).body,{code:null});
  await r.emit('hzPrepareForm','submit');r.el('hzAck').checked=true;await r.emit('hzBoughtOn','input');await r.emit('hzArm','click');assert.equal(r.requests.filter(x=>x.url.endsWith('/arm')).length,1);
  r.el('hzAfterHours').value='both';await r.emit('hzAfterHours','change');
  assert(!r.el('hzFloorFallback').disabled);r.el('hzFloorFallback').checked=true;r.el('hzRetryUnfilled').checked=true;
  await r.emit('hzPrepareForm','submit');assert.match(r.el('hzReview').innerHTML,/당일 하한가 허용/);
  assert.deepEqual(r.requests.filter(x=>x.url.endsWith('/prepare')).at(-1).body,{code:'005930',bought_on:'2026-09-21',after_hours:'both',retry_unfilled:true,empty_book:'floor'});
  await r.emit('hzFloorFallback','change');assert(!r.el('hzReviewDialog').open);
  r.el('hzAfterHours').value='off';await r.emit('hzAfterHours','change');assert(!r.el('hzFloorFallback').checked&&r.el('hzFloorFallback').disabled);
  r.events.offline();assert.match(r.el('hzStatus').textContent,/미확인/);
  console.log('PASS: mode switching never arms/pauses, shared stock/date, peer conflict, review/ack, invalidation, scoped stop, stale state and escaped names');
})().catch(e=>{console.error(e);process.exitCode=1;});
