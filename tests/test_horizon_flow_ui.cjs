// Isolated DOM and HTTP; tests cannot send a broker request.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const flush=()=>new Promise(r=>setImmediate(r));
const requests=[],root={innerHTML:'',setAttribute(){},remove(){this.parentNode=null}},slot={appendChild(el){el.parentNode=this}};
const document={hidden:false,createElement:()=>root,addEventListener(){}};
const window={};
vm.runInNewContext(fs.readFileSync('static/horizon-flow.js','utf8'),{window,document,AbortController,setTimeout,clearTimeout,setInterval(){},
 fetch:(url,options)=>{assert(url.startsWith('/api/horizon-flow/'));assert(!options.method&&!options.body);return new Promise(resolve=>requests.push({url,resolve,options}))}});
const row=(code='126340')=>({code,d0:'20260918',name:'선택 종목 '+code,price:12000});
function respond(i,extra={}){
 const u=new URL(requests[i].url,'http://fixture');
 requests[i].resolve({ok:true,json:async()=>({code:u.pathname.split('/').at(-1),since:u.searchParams.get('since'),at:'2026-09-23T10:00:00+09:00',connected:true,
 capture:{ticks:0,total_won:null,daily:[]},book:{},program:{},brokers:{},...extra})});
}
(async()=>{
 window.HorizonFlow.mount(slot,row());assert.equal(requests.length,1);
 window.HorizonFlow.mount(slot,row());assert.equal(requests.length,1);
 window.HorizonFlow.mount(slot,row('005930'));assert(requests[0].options.signal.aborted);
 respond(1,{program:{data:{date:'20260923',time:'100000',net_won:0,buy:0,sell:null,net:null}},
   brokers:{data:{days:['20260918','20260923'],bought:[{name:'<img src=x onerror=boom>',buy:100,sell:0,net:100}],sold:[],flips:[]}}});
 await flush();
 assert.match(root.innerHTML,/선택 종목 005930/);assert.match(root.innerHTML,/0.00억/);
 assert.match(root.innerHTML,/매도 —주/);assert.match(root.innerHTML,/&lt;img/);assert.doesNotMatch(root.innerHTML,/<img/);
 const saved=root.innerHTML;respond(0,{capture:{ticks:999,poc:12300,daily:[]}});await flush();
 assert.equal(root.innerHTML,saved);assert.doesNotMatch(root.innerHTML,/수신 체결 <b>999/);
 window.HorizonFlow.mount(slot,row('000660'));respond(2);await flush();
 assert.match(root.innerHTML,/기준봉 이후 체결 수집 자료가 없습니다/);assert.doesNotMatch(root.innerHTML,/0.00억/);
 window.HorizonFlow.mount(null,null);assert.equal(root.parentNode,null);
 console.log('PASS: quote-only GET, stock-switch cancellation, stale response rejection, escaped broker labels, unknown vs zero, empty capture');
})().catch(e=>{console.error(e);process.exitCode=1});
