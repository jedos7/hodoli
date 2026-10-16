// Read-only chart widget: fake DOM, canvas and HTTP; never calls any write API.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),core=require('../static/exit-chart-core.js');
const flush=()=>new Promise(r=>setImmediate(r));
function harness(){
  const elements=new Map(),requests=[],intervals=[],ctx=new Proxy({},{get:(o,k)=>o[k]||(()=>{}),set:(o,k,v)=>(o[k]=v,true)});
  const el=id=>{if(!elements.has(id))elements.set(id,{textContent:'',hidden:false,checked:true,disabled:false,dataset:{},children:[],handlers:{},style:{},
    classList:{toggle(){}},setAttribute(){},addEventListener(n,f){this.handlers[n]=f;},append(...v){this.children.push(...v);},
    replaceChildren(){this.children=[];},getBoundingClientRect(){return {width:1000,height:420,left:0};},getContext(){return ctx;},
    setPointerCapture(){},scrollIntoView(){},showModal(){},close(){this.handlers.close?.();}});return elements.get(id);};
  const frames=['1m','5m','15m','1d'].map(i=>{const b=el(i);b.dataset.chartInterval=i;return b;});
  const document={getElementById:el,createElement:()=>({style:{},append(){}}),querySelectorAll:()=>frames,addEventListener(){},hidden:false};
  const window={ExitChartCore:core,devicePixelRatio:1};
  const sandbox={window,document,AbortController,ResizeObserver:class{observe(){}},setTimeout,clearTimeout,setInterval:fn=>intervals.push(fn),console,
    fetch:(url,options)=>{assert(url.startsWith('/api/price-chart/'));assert(!options.method&&!options.body);return new Promise(resolve=>requests.push({url,resolve}));}};
  vm.runInNewContext(fs.readFileSync('static/custom-exit-chart.js','utf8'),sandbox);
  const respond=(index,{price=85000,error=false}={})=>{const req=requests[index],url=new URL(req.url,'http://fake');
    req.resolve({ok:!error,json:async()=>error?{detail:'가짜 조회 실패'}:{code:url.pathname.split('/').at(-1),interval:url.searchParams.get('interval'),
      source:'FAKE',fetched_at:'2026-09-23T11:30:00+09:00',last_bar_at:'20260923113000',
      bars:[{stamp:'20260923113000',open:price,high:price+100,low:price-100,close:price,volume:100}]}});};
  return {el,frames,requests,respond,api:window.ExitChart,intervals};
}
const plan=(code='126340')=>({code,name:'검증 종목 '+code,quantity:340,average:81217,status:'prepared',profit_protection:{price:'83800'}});
(async()=>{
  let r=harness();r.api.setPlans([plan()]);assert.equal(r.requests.length,1);
  r.respond(0);await flush();assert.match(r.el('exitChartOHLC').textContent,/85,000/);
  r.api.setPlans([plan()]);assert.equal(r.requests.length,1); // Two-second status polls do not query candles.
  r.api.setDraft({position:plan('005930'),inputs:{pp:{enabled:true,price:90000},tp:{enabled:false},sl:{enabled:false}}});
  r.api.setDraft({position:plan('000660'),inputs:{pp:{enabled:true,price:95000},tp:{enabled:false},sl:{enabled:false}}});
  r.respond(2,{price:96000});await flush();r.respond(1,{price:91000});await flush();
  assert.match(r.el('exitChartTitle').textContent,/000660/);assert.match(r.el('exitChartOHLC').textContent,/96,000/);
  assert.doesNotMatch(r.el('exitChartOHLC').textContent,/91,000/);
  r.api.setPlans([plan()]);assert.match(r.el('exitChartDialogContext').textContent,/입력값 미리보기/);
  r.frames[1].handlers.click();r.respond(3,{price:97000});await flush();
  assert.match(r.requests[3].url,/interval=5m/);assert.match(r.el('exitChartOHLC').textContent,/97,000/);
  const source=r.el('exitChartSource').textContent;
  r.el('exitChartRefresh').handlers.click();r.respond(4,{error:true});await flush();
  assert.match(r.el('exitChartMessage').textContent,/이전 수신 자료/);
  assert.equal(r.el('exitChartSource').textContent,source);assert.match(r.el('exitChartPending').textContent,/갱신 실패/);
  r.api.showPlan('126340');r.respond(5);await flush();assert.match(r.el('exitChartDialogContext').textContent,/저장된 조건.*시작 전/);
  r.api.setHorizon({...plan('005930'),source:'plan',method:'horizon',profit:11500,loss:9000});
  r.api.setMode('horizon');r.respond(6);await flush();
  assert.match(r.el('exitChartTitle').textContent,/005930/);
  r.api.setPlans([plan()]);assert.match(r.el('exitChartTitle').textContent,/005930/);
  r.api.setMode('custom');r.respond(7);await flush();assert.match(r.el('exitChartTitle').textContent,/126340/);
  assert(r.requests.every(x=>x.url.startsWith('/api/price-chart/')));
  console.log('PASS: chart-only GET requests, stale stock response rejection, timeframe switching, draft/plan context and stale data disclosure');
})().catch(e=>{console.error(e);process.exitCode=1;});
