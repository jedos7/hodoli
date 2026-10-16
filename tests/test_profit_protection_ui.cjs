// In-memory UI/API only. No socket or broker calls.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const clone=x=>JSON.parse(JSON.stringify(x));
const flush=()=>new Promise(r=>setImmediate(r));
function harness(){
  const elements=new Map(), requests=[];
  const el=id=>{
    if(!elements.has(id))elements.set(id,{value:'',checked:['#tpEnabled','#slEnabled'].includes(id),hidden:true,
      disabled:false,innerHTML:'',textContent:'',className:'',dataset:{},handlers:{},
      addEventListener(name,fn){this.handlers[name]=fn;},querySelectorAll(){return [];},
      closest(){return {classList:{toggle(){}}};},replaceChildren(){this.innerHTML='';},
      insertAdjacentHTML(pos,html){this.innerHTML+=html;}});
    return elements.get(id);
  };
  const position={code:'126340',name:'비나텍',quantity:220,available:220,average:81227};
  const state={environment:'paper',token:'FAKE',enabled:false,connected:true,market:'3',asof:'2026-09-23T10:00:00+09:00',error:'',plans:[],orders:[]};
  const rig={el,requests,state};
  const context=vm.createContext({console,Date,AbortSignal,setInterval(){},
    document:{querySelector:el,querySelectorAll:()=>[],addEventListener(){}},window:{addEventListener(){}},
    fetch:async(url,options)=>{
      const body=options.body?JSON.parse(options.body):null;
      requests.push({url,body});let data;
      if(url==='/api/custom-exit')data=clone(state);
      else if(url==='/api/custom-exit/holdings')data={positions:[position]};
      else if(url==='/api/custom-exit/prepare'){
        assert.equal(options.headers['X-Custom-Exit-Token'],'FAKE');
        const leg=(prefix,sign)=>body[prefix+'_pct']===null?null:{pct:body[prefix+'_pct'],quantity:body[prefix+'_quantity'],price:String(position.average*(1+sign*body[prefix+'_pct']/100))};
        const p={...position,id:'test-plan',environment:'paper',status:'prepared',watch_state:'prepared',note:'',
          take_profit:leg('take_profit',1),stop_loss:leg('stop_loss',-1),
          profit_protection:body.profit_protection_price===null?null:{price:String(body.profit_protection_price),quantity:220,comparison:'lt',all_remaining:true}};
        state.plans=[p];data=clone(p);
      }else throw Error('Unexpected write endpoint: '+url);
      return {ok:true,json:async()=>data};
    }});
  vm.runInContext(fs.readFileSync('static/custom-exit.js','utf8'),context);
  rig.load=async()=>{await el('#loadHoldings').handlers.click();await flush();el('#code').value='126340';};
  rig.change=()=>el('#prepareForm').handlers.change();
  rig.submit=async()=>{el('#prepareForm').handlers.submit({preventDefault(){}});await flush();};
  return rig;
}
(async()=>{
  const html=fs.readFileSync('static/custom-exit.html','utf8');
  // The legacy script remains compatible; the page now uses individual tickets.
  assert.match(html,/custom-exit-ticket.js/);assert.match(html,/id="triggerPrice"/);
  assert.match(html,/href="\/horizon.html"/);assert.match(html,/value="lt"/);
  let r=harness();await flush();
  assert.equal(r.el('#ppPrice').disabled,true);
  await r.load();
  r.el('#tpEnabled').checked=false;r.el('#slEnabled').checked=false;
  r.el('#ppEnabled').checked=true;r.el('#ppPrice').value='83800';r.change();
  assert.match(r.el('#ppPreview').textContent,/83,800원 미만.*220주 전량.*83,800원에서는 매도하지 않음/);
  await r.submit();
  const prepare=r.requests.find(x=>x.url.endsWith('/prepare'));
  assert.deepEqual(prepare.body,{code:'126340',take_profit_pct:null,take_profit_quantity:null,stop_loss_pct:null,stop_loss_quantity:null,profit_protection_price:83800});
  assert.match(r.el('#review').innerHTML,/83,800원 미만/);
  assert.match(r.el('#review').innerHTML,/같은 가격에서는 주문하지 않습니다/);
  assert.match(r.el('#review').innerHTML,/바로 매도할 수 있습니다/);
  assert.match(r.el('#review').innerHTML,/시장가/);
  assert.equal(r.el('#arm').disabled,true);
  assert(!r.requests.some(x=>x.url.endsWith('/arm')));
  assert.match(r.el('#plans').innerHTML,/이익보전 83,800원 미만.*220주 전량/);
  assert.match(r.el('#plans').innerHTML,/조건 취소/);
  r.state.orders=[{at:'2026-09-23T10:00:00+09:00',name:'비나텍',quantity:220,filled:0,status:'accepted',order_no:'fake',note:'',reason:'CUSTOM_PROFIT_PROTECTION',trigger_price:'83800',trigger_pct:null}];
  r.el('#ppPrice').value='83900';r.change();
  assert.equal(r.el('#review').hidden,true); // Edited inputs invalidate the reviewed plan.
  await r.submit();
  assert.match(r.el('#orders').innerHTML,/이익보전 83,800원 미만/);
  assert.doesNotMatch(r.el('#orders').innerHTML,/손절 −0%/);
  const calls=r.requests.length;r.el('#ppPrice').value='83800.5';await r.submit();
  assert.match(r.el('#error').textContent,/정수/);
  assert.equal(r.requests.length,calls);
  r=harness();await flush();await r.load();
  r.el('#tpPct').value='15';r.el('#tpQty').value='100';r.el('#slPct').value='10';r.el('#slQty').value='220';
  await r.submit();
  assert.equal(r.requests.find(x=>x.url.endsWith('/prepare')).body.profit_protection_price,null);
  assert.match(r.el('#review').innerHTML,/익절 \+15%/);assert.match(r.el('#review').innerHTML,/손절 −10%/);
  assert(!r.requests.some(x=>x.url.endsWith('/arm')));
  console.log('PASS: protection UI payload, strict boundary wording, all quantity, review gating, history, edit invalidation and legacy percentages');
})().catch(e=>{console.error(e);process.exitCode=1;});
