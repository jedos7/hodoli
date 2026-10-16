const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const flush=async()=>{for(let i=0;i<6;i++)await new Promise(r=>setImmediate(r));};
function harness(){
 const elements=new Map(),requests=[],timers=[];
 function node(){return {textContent:'',innerHTML:'',children:[],disabled:false,checked:false,hidden:false,open:false,handlers:{},addEventListener(n,f){this.handlers[n]=f;},append(c){this.children.push(c);},prepend(c){this.children.unshift(c);},remove(){},get lastElementChild(){return this.children.at(-1);},showModal(){this.open=true;},close(){this.open=false;}};}
 const el=id=>{if(!elements.has(id))elements.set(id,node());return elements.get(id);};
 const window={},context={window,document:{getElementById:el,createElement:node},Date,AbortSignal,console,setInterval:f=>timers.push(f),fetch:async(url,opt)=>{
  const body=JSON.parse(opt.body);requests.push({url,body});
  const data=url.endsWith('/prepare')?{review_id:url.includes('autoexit')?'HZ':'CUSTOM',environment:'real',items:url.includes('autoexit')?[{id:'o1',name:'검증 <종목>',venue:'NXT',order_no:'101',remaining:7,filled:3,quantity:10}]:[],excluded:[]}:{results:[{state:'accepted',note:'취소 접수 · 완료 확인 대기'}]};
  return {ok:true,json:async()=>data};
 }};
 vm.runInNewContext(fs.readFileSync('static/exit-order-tools.js','utf8'),context);
 return {el,requests,api:window.ExitOrderTools,async emit(id,event='click'){await el(id).handlers[event]();await flush();}};
}
const base=orders=>({token:'FAKE',orders,environment:'real'}),order=(filled,status='partial')=>({id:'o1',name:'테스트',quantity:10,remaining:10-filled,filled,status});
(async()=>{
 const r=harness();r.api.update('horizon',base([order(2)]));r.api.update('custom',base([]));
 assert.equal(r.el('orderNotifications').children.length,0);assert.equal(r.requests.length,0);
 r.api.update('horizon',base([order(3)]));assert.equal(r.el('orderNotifications').children.length,1);
 assert.match(r.el('orderNotifications').children[0].textContent,/미체결 7주/);
 r.api.update('horizon',base([order(3)]));assert.equal(r.el('orderNotifications').children.length,1);
 r.api.update('horizon',base([order(10,'filled')]));assert.equal(r.el('orderNotifications').children.length,2);
 await r.emit('reviewSellCancel');assert(r.el('cancelReviewDialog').open);
 assert.equal(r.requests.length,2);assert(r.requests.every(x=>x.url.endsWith('/prepare')));
 assert.match(r.el('cancelTargets').innerHTML,/&lt;종목&gt;/);assert(r.el('cancelConfirm').disabled);
 await r.emit('cancelConfirm');assert.equal(r.requests.length,2);
 r.el('cancelAck').checked=true;await r.emit('cancelAck','change');await r.emit('cancelConfirm');
 assert.equal(r.requests.length,3);assert.equal(r.requests[2].url,'/api/autoexit/cancel/confirm');
 assert.deepEqual(r.requests[2].body,{review_id:'HZ',environment:'real',confirm:true});
 assert.match(r.el('cancelTargets').textContent,/취소 접수/);assert.match(r.el('cancelError').textContent,/완료가 아닙니다/);
 await r.emit('cancelConfirm');assert.equal(r.requests.length,3);
 console.log('PASS: historical fills silent, partial/full fill notifications once, remaining quantity, cancel preview read-only, explicit ack, exact source, no duplicate submit');
})().catch(e=>{console.error(e);process.exitCode=1;});
