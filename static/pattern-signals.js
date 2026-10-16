/* Theme-scoped observation panel. Render values as text, never executable HTML. */
(() => {
  const root=document.getElementById('patternSignals');
  let active=null,rows=[],request=0,busy=false;
  root.innerHTML=`<h3>역매공파112 · 256 관찰</h3>
    <p>당일 봉 제외 · 설정 기반 신호 · 볼린저 상단은 별도 참고값</p>
    <div class="pattern-controls">
      <label>거래량 배수 <input id="psVolume" type="number" min="1" max="10" step=".1" value="2"></label>
      <label>횡보폭 % <input id="psWidth" type="number" min="1" max="50" step="1" value="12"></label>
      <label>112선 접근 % <input id="psNear" type="number" min=".5" max="20" step=".5" value="3"></label>
      <button id="psLoad" type="button">일봉 신호 조회</button>
      <label>112 단계 <select id="psStage"><option value="">전체</option><option>데이터 부족</option><option>조회 실패</option><option>조건 미충족</option><option>역배열 관찰</option><option>거래량봉 관찰</option><option>횡보·지지 확인</option><option>20·60선 회복</option><option>112선 접근</option><option>112선 돌파</option><option>112선 위 관찰</option><option>112선 안착</option><option>지지 이탈</option></select></label>
      <label>256 <select id="psShort"><option value="">전체</option><option>반등확인</option><option>눌림관찰</option><option>관찰</option><option>추세약화</option><option>데이터 부족</option><option>조회 실패</option></select></label>
      <label>조건 <select id="psCondition"><option value="">전체</option><option value="inverse">현재 역배열</option><option value="spike">거래량봉</option><option value="base">횡보·지지</option><option value="recovery">20·60선 회복</option><option value="volume256">256 거래량 확인</option></select></label>
      <label>정렬 <select id="psSort"><option value="rank">112 단계순</option><option value="volume_ratio">거래량 배수순</option><option value="distance112">112선 거리순</option><option value="name">종목명순</option></select></label>
    </div><p id="psStatus" role="status"></p><div id="psRows"></div>`;
  const el=id=>document.getElementById(id);
  const num=(v,d=1)=>Number.isFinite(v)?v.toFixed(d):'—';
  function render(){
    const list=rows.filter(r=>(!el('psStage').value||r.stage===el('psStage').value)&&(!el('psShort').value||r.signal256===el('psShort').value)&&(!el('psCondition').value||r.conditions?.[el('psCondition').value]));
    const k=el('psSort').value;
    list.sort((a,b)=>k==='name'?a.name.localeCompare(b.name,'ko'):k==='distance112'?(Math.abs(a[k]??Infinity)-Math.abs(b[k]??Infinity)):((b[k]??-Infinity)-(a[k]??-Infinity)));
    el('psRows').replaceChildren();
    for(const r of list){
      const item=document.createElement('details');
      const title=document.createElement('summary');
      title.textContent=`${r.name} · ${r.stage} / ${r.signal256}`;
      const detail=document.createElement('p');
      const s=active?.stocks.find(s=>s.code===r.code);
      detail.textContent=`${r.code} · 현재가 ${s?.price?.toLocaleString('ko-KR')??'—'}원 · 일봉 기준 ${r.asof??'없음'} · ${r.bars??0}봉\n거래량 ${num(r.volume_ratio)}배 · 112선 거리 ${num(r.distance112)}% · 볼린저 상단 ${num(r.bb_upper,0)}\n거래량봉 ${r.spike_date??'없음'} · 지지 기준 ${num(r.support,0)}\n이평 ${Object.entries(r.averages??{}).map(([p,v])=>`${p}일 ${num(v,0)}`).join(' / ')}\n조회 ${r.fetched_at??'실패'}\n112 분석에는 최소 448봉이 필요합니다. 매수·매도 보장 신호가 아닙니다.`;
      item.append(title,detail);el('psRows').append(item);
    }
    if(!list.length)el('psRows').textContent=rows.length?'필터에 맞는 종목이 없습니다.':'일봉 신호를 조회하면 표시됩니다.';
  }
  async function load(){
    if(!active||busy)return;
    const inputs=['psVolume','psWidth','psNear'].map(el);
    if(inputs.some(i=>!i.reportValidity()))return;
    busy=true;el('psLoad').disabled=true;
    const id=++request,theme=active.id;
    el('psStatus').textContent='일봉 조회 중… 테마 종목 수에 따라 시간이 걸립니다.';
    try{
      const q=new URLSearchParams({spike_ratio:inputs[0].value,base_width:+inputs[1].value/100,near112:+inputs[2].value/100});
      const response=await fetch(`/api/pattern-signals/${encodeURIComponent(theme)}?${q}`);
      if(!response.ok)throw Error(`조회 오류 ${response.status}`);
      const data=await response.json();
      if(id!==request)return;
      rows=data.rows;render();
      el('psStatus').textContent=`${data.source} · ${data.policy} · ${rows.length}종목 · 조회 실패 ${rows.filter(r=>r.stage==='조회 실패').length}개`;
    }catch(e){if(id===request)el('psStatus').textContent=e.message;}
    finally{if(id===request){busy=false;el('psLoad').disabled=false;}}
  }
  el('psLoad').addEventListener('click',load);
  ['psStage','psShort','psCondition','psSort'].forEach(id=>el(id).addEventListener('change',render));
  window.PatternSignals={show(theme){
    if(active?.id===theme.id){active=theme;return;}
    active=theme;rows=[];request++;busy=false;el('psLoad').disabled=false;
    el('psStatus').textContent=`${theme.name} · 일봉 조회 대기`;render();
  }};
  if(typeof themes!=='undefined'&&typeof selected!=='undefined'){
    const th=themes.find(t=>t.id===selected);if(th)window.PatternSignals.show(th);
  }
})();
