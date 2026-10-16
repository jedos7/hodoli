// In-memory UI logic regression; no browser, network or broker connection.
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const source=fs.readFileSync('static/autoexit.js','utf8');
const elements=new Map(), intervals=new Map(), events={};
const badge={textContent:'',className:''};
const el=id=>{if(!elements.has(id))elements.set(id,{textContent:'',innerHTML:'',className:'',value:'',
  addEventListener(){},querySelectorAll:()=>id==='#plans'?[badge]:[]});return elements.get(id)};
let clock=Date.parse('2026-09-22T10:00:00+09:00'), fail=false;
let state={environment:'paper',enabled:true,connected:true,market:'',asof:new Date(clock).toISOString(),error:'',
  plans:[{code:'005930',name:'가짜 테스트 종목',status:'armed',quantity:10,average:10000,bought_on:'2026-09-18',note:'장 상태 대기'}],orders:[]};
class Clock extends Date {static now(){return clock;}}
const context=vm.createContext({console,Date:Clock,AbortSignal,setInterval:(fn,ms)=>intervals.set(ms,fn),
  document:{querySelector:el,querySelectorAll:()=>[],addEventListener:(n,fn)=>events[n]=fn},
  window:{addEventListener:(n,fn)=>events[n]=fn},
  fetch:async(url,options)=>{assert.equal(options.method,'GET');if(fail)throw Error('offline');
    return {ok:true,json:async()=>url==='/api/autoexit'?state:{positions:[]}};}});
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  vm.runInContext(source,context);await flush();
  assert.match(el('#plans').innerHTML,/감시 대기/);
  assert.doesNotMatch(el('#plans').innerHTML,/pill armed/);
  state.market='3';state.plans[0].note='켜짐 · 최신 KRX 체결가 대기';
  await intervals.get(2000)();assert.match(el('#plans').innerHTML,/시세 대기/);
  state.plans[0].note='익절·손절 감시 중';
  await intervals.get(2000)();assert.match(el('#plans').innerHTML,/pill armed/);
  assert.equal(el('#planCount').textContent,'1종목 감시');
  state.connected=false;await intervals.get(2000)();
  assert.match(el('#plans').innerHTML,/연결 확인 필요/);
  assert.doesNotMatch(el('#plans').innerHTML,/pill armed/);
  state.connected=true;await intervals.get(2000)();
  fail=true;await intervals.get(2000)();
  assert.equal(badge.textContent,'상태 미확인');
  assert.equal(el('#status').textContent,'연결 확인 필요');
  fail=false;await intervals.get(2000)();assert.match(el('#plans').innerHTML,/pill armed/);
  clock+=11000;intervals.get(1000)();assert.equal(badge.textContent,'상태 미확인');
  assert.equal(el('#status').textContent,'연결 확인 필요');
  console.log('PASS: session/quote/connection waiting, active state, offline recovery, stale status; zero POST requests');
})().catch(e=>{console.error(e);process.exitCode=1});
