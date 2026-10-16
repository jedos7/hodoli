// In-memory API only. No process, browser session or broker access.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const flush=()=>new Promise(r=>setImmediate(r)),clone=x=>JSON.parse(JSON.stringify(x));
function harness(){
 const els=new Map(),requests=[],intervals=new Map();
 const el=id=>{if(!els.has(id))els.set(id,{value:'',checked:false,hidden:true,disabled:false,innerHTML:'',textContent:'',dataset:{},events:{},
  classList:{toggle(){},remove(){}},querySelectorAll(){return [];},addEventListener(n,fn){this.events[n]=fn;},scrollIntoView(){}});return els.get(id);};
 const r={el,requests,intervals,fail:false,deferred:null,state:{token:'FAKE',environment:'paper',connected:true,enabled:false,
  market:'3',plans:[],orders:[],error:'',asof:'2026-09-22T10:00:00+09:00'}};
 const ctx=vm.createContext({console,Date,Intl,AbortSignal,setInterval:(fn,ms)=>intervals.set(ms,fn),
  document:{getElementById:id=>id==='confirmExit'&&!el('review').innerHTML.includes('id="confirmExit"')?null:el(id),querySelectorAll:()=>[]},
  fetch:async(url,options)=>{
   const body=options.body?JSON.parse(options.body):null;requests.push({url,body,options});
   if(r.fail)throw Error('offline');
   if(options.method==='GET'){
    if(r.deferred){const d=r.deferred;r.deferred=null;return d;}
    return {ok:true,json:async()=>clone(r.state)};
   }
   assert.equal(options.headers['X-Auto-Buy-Token'],'FAKE');
   if(url.endsWith('/prepare')){
    if(r.prepareError)return {ok:false,json:async()=>({detail:r.prepareError})};
    const p={...body,id:'plan-1',name:'<가짜 종목>',environment:'paper',status:'prepared',watch_state:'prepared',
      note:'fake',take_profit:body.take_profit_pct?{pct:body.take_profit_pct}:null,stop_loss:body.stop_loss_pct?{pct:body.stop_loss_pct}:null};
    r.state.plans=[p];return {ok:true,json:async()=>clone(p)};
   }
   if(url.endsWith('/arm')){const p=r.state.plans[0];assert.equal(body.plan_id,p.id);p.status='armed';p.watch_state='watching';r.state.enabled=true;}
   else if(url.endsWith('/pause')){r.state.plans.forEach(p=>{p.status='paused';p.watch_state='paused'});r.state.enabled=false;}
   else if(url.endsWith('/cancel')){r.state.orders[0].cancel={status:'accepted'};}
   else if(url.endsWith('/remove')){r.state.plans=[];}
   else assert.fail('Unexpected request '+url);
   return {ok:true,json:async()=>clone(r.state)};
  }});
 vm.runInContext(fs.readFileSync('static/auto-buy.js','utf8'),ctx);return r;
}
async function prepare(r,link=false){
 r.el('code').value='005930';r.el('limitPrice').value='260000';r.el('quantity').value='100';r.el('expiresOn').value='2026-09-22';
 r.el('linkExit').checked=link;r.el('tpPct').value=link?'15':'';r.el('slPct').value=link?'10':'';
 r.el('prepareForm').events.submit({preventDefault(){}});await flush();
}
(async()=>{
 let r=harness();await flush();assert.equal(r.requests.length,1);assert.equal(r.requests[0].options.method,'GET');
 await prepare(r,true);assert.equal(r.requests.at(-1).url,'/api/auto-buy/prepare');
 assert.equal(r.requests.at(-1).body.max_amount,26000000);assert.equal(r.el('arm').disabled,true);
 assert.match(r.el('review').innerHTML,/&lt;가짜 종목&gt;/);assert.doesNotMatch(r.el('review').innerHTML,/<가짜 종목>/);
 r.el('confirmLimit').checked=true;r.el('confirmLimit').events.change();assert.equal(r.el('arm').disabled,true);
 r.el('confirmExit').checked=true;r.el('confirmExit').events.change();assert.equal(r.el('arm').disabled,false);
 await r.el('arm').onclick();await flush();assert.equal(r.requests.at(-1).body.confirm_exit,true);
 assert.equal(r.state.plans[0].status,'armed');
 await r.el('stopAll').onclick();await flush();assert.equal(r.requests.at(-1).body.code,null);assert.equal(r.state.enabled,false);
 assert.match(r.el('feedback').textContent,/이미 접수된 주문/);

 r=harness();await flush();await prepare(r);r.el('prepareForm').events.input();
 assert.equal(r.el('review').hidden,true);assert.equal(r.requests.filter(x=>x.url.endsWith('/arm')).length,0);

 r=harness();await flush();await prepare(r);r.state.plans[0].status='armed';r.state.plans[0].watch_state='watching';r.state.enabled=true;
 const old=clone(r.state);let resolve;r.deferred=new Promise(done=>resolve=done);
 const poll=r.intervals.get(3000)();await flush();await r.el('stopAll').onclick();await flush();
 resolve({ok:true,json:async()=>old});await poll;assert.equal(r.el('status').textContent,'자동매수 꺼짐');
 r.fail=true;await r.intervals.get(3000)();assert.match(r.el('status').textContent,/미확인/);
 assert.equal(r.requests.filter(x=>x.url.endsWith('/arm')).length,0);

 r=harness();await flush();r.state.orders=[{id:'o1',name:'가짜',code:'005930',order_no:'123',quantity:100,filled:40,remaining:60,status:'partial',limit_price:260000,at:r.state.asof,note:'일부 체결'}];
 await r.intervals.get(3000)();r.el('orders').onclick({target:{closest:()=>({dataset:{cancel:'o1'}})}});
 assert.equal(r.requests.filter(x=>x.url.endsWith('/cancel')).length,0,'Opening cancellation review must not send');
 await r.el('confirmCancel').onclick();await flush();assert.equal(r.requests.at(-1).url,'/api/auto-buy/cancel');
 assert.equal(r.state.orders[0].status,'partial','Cancel acceptance must not imply filled/closed');
 
 // A successful status poll must not erase a rejected prepare request.
 r=harness();await flush();
 r.prepareError='매수 후 자동매도 연결은 기존 보유가 없는 종목만 가능합니다 · 추가 매수는 연결 없이 설정하세요';
 r.el('code').value='144960';r.el('limitPrice').value='12680';r.el('quantity').value='100';
 r.el('expiresOn').value='2026-09-23';r.el('linkExit').checked=true;r.el('linkExit').onchange();
 r.el('tpPct').value='15';r.el('slPct').value='10';
 r.el('prepareForm').events.submit({preventDefault(){}});
 assert.equal(r.el('prepareProgress').hidden,false);
 assert.match(r.el('prepare').textContent,/확인 중/);
 await flush();
 assert.equal(r.requests.at(-1).body.max_amount,1268000);
 assert.equal(r.el('prepareError').textContent,r.prepareError);
 assert.equal(r.el('prepareError').hidden,false);
 assert.equal(r.el('prepareProgress').hidden,true);
 assert.equal(r.el('prepare').disabled,false);
 await r.intervals.get(3000)();await r.intervals.get(3000)();
 assert.equal(r.el('prepareError').textContent,r.prepareError);
 assert.equal(r.el('error').textContent,r.prepareError);
 assert.equal(r.requests.filter(x=>x.url.endsWith('/arm')).length,0);
 assert.equal(r.state.plans.length,0);
 // Only an explicit user change disables attached exits; never change it for them.
 assert.equal(r.el('linkExit').checked,true);
 r.el('linkExit').checked=false;r.el('linkExit').onchange();
 assert.equal(r.el('prepareError').hidden,true);
 assert.equal(r.el('tpPct').disabled,true);assert.equal(r.el('slPct').disabled,true);
 r.prepareError='';r.el('prepareForm').events.submit({preventDefault(){}});await flush();
 assert.equal(r.requests.at(-1).body.take_profit_pct,null);
 assert.equal(r.requests.at(-1).body.stop_loss_pct,null);
 assert.equal(r.el('review').hidden,false);
 assert.match(r.el('review').innerHTML,/1,268,000/);
 assert.equal(r.requests.filter(x=>x.url.endsWith('/arm')).length,0);
 r.el('linkExit').checked=true;r.el('linkExit').onchange();
 assert.equal(r.el('tpPct').disabled,false);assert.equal(r.el('slPct').disabled,false);
 // Native form validation also gets visible feedback without any API call.
 const requestCount=r.requests.length;
 r.el('prepareForm').events.invalid({target:{id:'quantity',validationMessage:'수량을 입력하세요.'}});
 assert.match(r.el('prepareError').textContent,/매수 수량: 수량을 입력하세요/);
 assert.equal(r.requests.length,requestCount);
 const html=fs.readFileSync('static/auto-buy.html','utf8');
 assert.match(html,/id="tpPct" type="number" disabled/);
 assert.match(html,/id="slPct" type="number" disabled/);
 assert.match(html,/id="prepareError" class="alert" role="alert" hidden/);
 console.log('PASS: no startup orders, exact amount, separate buy/exit confirmations, edit invalidation, stale response guard, offline state, two-step cancellation, HTML escaping.');
 console.log('PASS: 409 feedback survives polling, loading feedback, explicit unlink, review without arming, visible native validation.');
})().catch(e=>{console.error(e);process.exitCode=1});
