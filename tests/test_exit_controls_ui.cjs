// Run both real UI scripts against an in-memory API; never contacts a broker.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const clone=x=>JSON.parse(JSON.stringify(x));
const flush=()=>new Promise(r=>setImmediate(r));
function harness(custom){
  const prefix=custom?'/api/custom-exit':'/api/autoexit';
  const elements=new Map(),intervals=new Map(),requests=[];
  const el=id=>{
    if(!elements.has(id))elements.set(id,{value:'',checked:id.endsWith('Enabled'),hidden:true,disabled:false,innerHTML:'',textContent:'',className:'',dataset:{},handlers:{},
      addEventListener(name,fn){this.handlers[name]=fn;},querySelectorAll(){return [];},
      closest(){return {classList:{toggle(){}}};},replaceChildren(){this.innerHTML='';},
      insertAdjacentHTML(pos,html){this.innerHTML+=html;}});
    return elements.get(id);
  };
  const row=(code)=>({id:code,code,name:'가짜 테스트 종목',status:'armed',watch_state:'watching',continue_stop:true,
    quantity:200,average:10000,bought_on:'2026-09-18',take_profit:{pct:5,quantity:100,price:'10500'},stop_loss:{pct:3,quantity:200,price:'9700'},note:''});
  const rig={el,intervals,requests,prefix,failPause:false,deferred:null,
    state:{token:'FAKE',environment:'paper',enabled:true,connected:true,market:'3',asof:'2026-09-22T10:00:00+09:00',error:'',plans:[row('005930'),row('000660')],orders:[]}};
  const context=vm.createContext({console,Date,AbortSignal,setInterval:(fn,ms)=>intervals.set(ms,fn),
    document:{querySelector:el,querySelectorAll:()=>[],addEventListener(){}},window:{addEventListener(){}},
    fetch:async(url,options)=>{
      requests.push({url,options,body:options.body?JSON.parse(options.body):null});
      if(options.method==='GET'){
        if(url===prefix && rig.deferred){const d=rig.deferred;rig.deferred=null;return await d;}
        const data=url===prefix?clone(rig.state):{positions:[]};return {ok:true,json:async()=>data};
      }
      if(!custom&&url===prefix+'/remove'){
        const {code,plan_id}=JSON.parse(options.body);
        const p=rig.state.plans.find(p=>p.code===code&&p.id===plan_id);
        assert(p&&p.status==='paused');
        rig.state.plans=rig.state.plans.filter(p=>p.id!==plan_id);
        const data={...clone(rig.state),deleted_code:code,deleted_plan_id:plan_id};
        return {ok:true,json:async()=>data};
      }
      assert.equal(url,prefix+'/pause','Only the owning feature pause endpoint is allowed');
      assert.equal(options.headers[custom?'X-Custom-Exit-Token':'X-Autoexit-Token'],'FAKE');
      if(rig.failPause)throw Error('simulated lost response');
      const {code}=JSON.parse(options.body);
      for(const p of rig.state.plans){if(code&&p.code!==code)continue;
        if(custom)p.continue_stop=false;
        if(['prepared','armed','checking','remainder_check'].includes(p.status))p.status='paused';
        p.watch_state=p.status;
      }
      rig.state.enabled=rig.state.plans.some(p=>['armed','checking','remainder_check'].includes(p.status)||(custom&&p.status==='ordered'&&p.continue_stop));
      const data=clone(rig.state);return {ok:true,json:async()=>data};
    }});
  vm.runInContext(fs.readFileSync('static/'+(custom?'custom-exit':'autoexit')+'.js','utf8'),context);
  rig.cancel=async(code,review=false)=>{el(review?'#review':'#plans').handlers.click({target:{closest:selector=>selector==='[data-pause]'?{dataset:{pause:code}}:null}});await flush();};
  rig.remove=async(code)=>{el('#plans').handlers.click({target:{closest:selector=>selector==='[data-remove]'?{dataset:{remove:code,planId:code}}:null}});await flush();};
  rig.all=async()=>{await el('#stopAll').handlers.click();await flush();};
  return rig;
}

(async()=>{
  for(const custom of [false,true]){
    let r=harness(custom);await flush();
    assert.match(r.el('#plans').innerHTML,/조건 취소 \(감시 끄기\)/);
    await r.cancel('005930');
    assert.equal(r.state.plans[0].status,'paused');assert.equal(r.state.plans[1].status,'armed');
    assert.equal(r.state.enabled,true);assert.match(r.el('#feedback').textContent,/005930.*감시를 껐습니다/);
    assert.match(r.el('#plans').innerHTML,/조건 감시 꺼짐/);
    await r.all();assert.equal(r.state.enabled,false);assert.equal(r.requests.filter(x=>x.options.method==='POST').at(-1).body.code,null);

    r=harness(custom);await flush();r.state.plans[0].status='prepared';
    await r.cancel('005930',true);assert.equal(r.state.plans[0].status,'paused');assert.equal(r.el('#review').hidden,true);

    r=harness(custom);await flush();
    r.state.plans[0].status='ordered';r.state.orders=[{code:'005930',name:'가짜',status:'partial',quantity:100,filled:20,at:'2026-09-22T10:00:00+09:00',reason:custom?'CUSTOM_TAKE_PROFIT':'TAKE_PROFIT',trigger_pct:5,note:'접수됨'}];
    await r.all();assert.equal(r.state.orders[0].status,'partial');
    assert.match(r.el('#feedback').textContent,/주문은 취소되지 않았습니다/);
    assert.match(r.el('#plans').innerHTML,/접수된 주문은 키움에서 별도 확인/);
    if(custom)assert.equal(r.state.plans[0].continue_stop,false);

    r=harness(custom);await flush();r.failPause=true;await r.cancel('005930');
    assert.equal(r.el('#feedback').hidden,true);assert.match(r.el('#error').textContent,/취소 응답 미확인/);
    assert.equal(r.state.plans[0].status,'armed');

    r=harness(custom);await flush();const old=clone(r.state);let resolve;
    r.deferred=new Promise(done=>resolve=done);const polling=r.intervals.get(2000)();await flush();
    await r.all();const cancelledHTML=r.el('#plans').innerHTML;
    resolve({ok:true,json:async()=>old});await polling;
    assert.equal(r.el('#plans').innerHTML,cancelledHTML,'Delayed status must not revive cancelled controls');
    assert.doesNotMatch(r.el('#plans').innerHTML,/data-pause=/);
    assert(r.requests.every(x=>x.options.method==='GET'||x.url===r.prefix+'/pause'));
    console.log(`PASS ${custom?'custom':'horizon'}: individual/all/review cancellation, pending order disclosure, failed response, delayed status; no trading requests`);
  }
  const r=harness(false);await flush();await r.cancel('005930');
  assert.match(r.el('#plans').innerHTML,/data-remove="005930"/);
  await r.remove('005930');
  assert.equal(r.state.plans.length,1);assert.equal(r.state.plans[0].code,'000660');assert.equal(r.state.plans[0].status,'armed');
  assert.match(r.el('#feedback').textContent,/저장 목록에서 삭제/);
  assert.doesNotMatch(r.el('#plans').innerHTML,/005930/);
  console.log('PASS: inactive condition deletion uses exact plan ID and preserves the other active plan');
})().catch(e=>{console.error(e);process.exitCode=1});
