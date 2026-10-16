// Runs only the UI against an in-memory DOM and fake API. No browser/broker sockets.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const flush=async()=>{for(let i=0;i<12;i++)await new Promise(r=>setImmediate(r));};
function harness(){
  const elements=new Map(),requests=[],timers=[];
  const el=id=>{if(!elements.has(id))elements.set(id,{id,value:'',checked:false,disabled:false,hidden:false,open:false,textContent:'',innerHTML:'',className:'',dataset:{},handlers:{},
    classList:{toggle(){}},setAttribute(){},replaceChildren(){this.innerHTML='';},showModal(){this.open=true;},close(){this.open=false;},scrollIntoView(){},
    addEventListener(n,f){(this.handlers[n]??=[]).push(f);}});return elements.get(id);};
  const emit=async(id,event,args={})=>{for(const f of el(id).handlers[event]||[])await f({target:el(id),preventDefault(){},...args});await flush();};
  el('allRemaining').checked=true;el('comparison').value='lt';el('orderType').value='market';el('expiryMode').value='today';
  const presets=['profit','protection','loss'].map(kind=>{const e=el(kind);e.dataset.kind=kind;return e;});
  const p={code:'126340',name:'테스트 <script>',quantity:130,available:130,average:82508};
  const rule={id:'legacy-profit_protection',kind:'protection',comparison:'lt',price:'83800',quantity:130,all_remaining:true,order_type:'market',limit_price:null,expires_on:null,enabled:false,state:'paused'};
  let state={environment:'paper',token:'FAKE',enabled:false,connected:true,market:'3',error:'',asof:new Date().toISOString(),orders:[],plans:[{...p,id:'old',mode:'custom',status:'paused',watch_state:'paused',conditions:[rule]}]};
  const document={getElementById:el,activeElement:null,hidden:false,querySelectorAll:s=>s==='[data-kind]'?presets:[],addEventListener(){}};
  let offline;
  const sandbox={document,window:{ExitChart:{setPlans(){},setTicketDraft(){},showPlan(){}},addEventListener:(n,f)=>{if(n==='offline')offline=f;}},AbortSignal,Intl,Date,console,
    setInterval:f=>timers.push(f),fetch:async(url,opts)=>{
      const body=opts.body?JSON.parse(opts.body):undefined;requests.push({url,body});
      let data=state;
      if(url.endsWith('/holdings'))data={positions:[p]};
      if(url.endsWith('/tickets/prepare'))data={...p,environment:'paper',review_id:'review1',prepared_at:new Date().toISOString(),rule:{...body,id:'new',enabled:false,state:'paused'}};
      return {ok:true,json:async()=>JSON.parse(JSON.stringify(data))};
    }};
  vm.runInNewContext(fs.readFileSync('static/custom-exit-ticket.js','utf8'),sandbox);
  return {el,emit,requests,timers,offline:()=>offline(),p,rule};
}
(async()=>{
  const html=fs.readFileSync('static/custom-exit.html','utf8');
  for(const id of ['afterHours','hzAfterHours']) {assert(html.includes('type="hidden" id="'+id+'" value="both"'));assert(!html.includes('<select id="'+id+'"'));}
  for(const id of ['triggerPrice','sellQuantity','comparison','expiryDate','orderType','limitPrice','reviewDialog'])assert(html.includes(`id="${id}"`));
  let r=harness();await flush();assert(r.requests.every(x=>x.body===undefined));
  assert.match(r.el('plans').innerHTML,/83,800원 미만.*130주/);assert.match(r.el('plans').innerHTML,/&lt;script&gt;/);
  await r.emit('loadHoldings','click');r.el('code').value='126340';
  await r.emit('profit','click');assert.equal(r.el('triggerPrice').value,'94884.2');
  r.el('comparison').value='gte';r.el('triggerPrice').value='95200';r.el('orderType').value='limit';r.el('limitPrice').value='95200';
  r.el('allRemaining').checked=false;r.el('sellQuantity').value='91';r.el('expiryMode').value='date';r.el('expiryDate').value='2026-10-07';
  await r.emit('prepareForm','change');await r.emit('prepareForm','submit');
  const req=r.requests.find(x=>x.url.endsWith('/tickets/prepare'));
  assert.deepEqual(req.body,{code:'126340',kind:'profit',price:'95200',comparison:'gte',quantity:91,all_remaining:false,order_type:'limit',limit_price:95200,expires_on:'2026-10-07',rule_id:null,after_hours:'both',retry_unfilled:false,empty_book:'wait'});
  assert.equal(r.el('reviewDialog').open,true);assert.match(r.el('review').innerHTML,/95,200원 지정가/);assert.match(r.el('review').innerHTML,/2026-10-07/);
  assert(!r.requests.some(x=>x.url.endsWith('/tickets/arm')));
  await r.emit('arm','click');assert(!r.requests.some(x=>x.url.endsWith('/tickets/arm')));
  r.el('ack').checked=true;await r.emit('ack','change');await r.emit('arm','click');
  assert.equal(r.requests.filter(x=>x.url.endsWith('/tickets/arm')).length,1);
  assert.deepEqual(r.requests.find(x=>x.url.endsWith('/tickets/arm')).body,{review_id:'review1',environment:'paper',confirm_order:true});
  await r.emit('prepareForm','submit');r.el('ack').checked=true;await r.emit('prepareForm','input');await r.emit('arm','click');
  assert.equal(r.requests.filter(x=>x.url.endsWith('/tickets/arm')).length,1);assert.equal(r.el('reviewDialog').open,false);
  await r.emit('stopAll','click');assert.deepEqual(r.requests.find(x=>x.url.endsWith('/pause')).body,{code:null});
  r.offline();assert.match(r.el('status').textContent,/확인 필요/);
  r=harness();await flush();await r.emit('loadHoldings','click');r.el('code').value='126340';
  r.el('afterHours').value='both';r.el('retryUnfilled').checked=true;
  r.el('triggerPrice').value='83800';await r.emit('prepareForm','change');
  assert.equal(r.el('retryUnfilled').disabled,false);assert.equal(r.el('floorFallback').checked,false);
  await r.emit('prepareForm','submit');
  assert.match(r.el('review').innerHTML,/08:00~20:00 · 시장 자동 선택/);
  assert.match(r.el('review').innerHTML,/취소 완료/);assert.match(r.el('review').innerHTML,/최대 5회/);
  assert.match(r.el('review').innerHTML,/매수호가가 없으면 주문 대기/);
  assert(!r.requests.some(x=>x.url.endsWith('/tickets/arm')));
  await r.emit('profit','click');assert.equal(r.el('retryUnfilled').checked,false);assert.equal(r.el('retryUnfilled').disabled,true);
  r.el('floorFallback').checked=true;r.el('orderType').value='limit';await r.emit('prepareForm','change');
  assert.equal(r.el('floorFallback').checked,false);assert.equal(r.el('floorFallback').disabled,true);
  r.el('afterHours').value='off';await r.emit('prepareForm','change');
  assert.equal(r.el('retryUnfilled').checked,false);assert.equal(r.el('floorFallback').checked,false);
  console.log('PASS: ticket payload, escaped stock names, strict below legacy display, independent review/ack, edit invalidation, stop and stale state');
  console.log('PASS: extended venue, cancel-confirm review, 5 retries, floor opt-in, profit retry disabled, no implicit arming');
})().catch(e=>{console.error(e);process.exitCode=1;});
