(() => {
  'use strict';
  const $ = s => document.querySelector(s);
  const esc = s => String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number = (v,d=0) => v==null || !Number.isFinite(Number(v)) ? '—' : Number(v).toLocaleString('ko-KR',{minimumFractionDigits:d,maximumFractionDigits:d});
  const percent = v => {if(v==null)return '—';const n=Math.abs(Number(v))<.005?0:Number(v);return `${n>0?'+':''}${number(n,2)}%`};
  const sign = v => v>0?'up':v<0?'down':'muted';
  const time = v => v ? new Date(v).toLocaleTimeString('ko-KR',{hour12:false,timeZone:'Asia/Seoul'}) : '—';
  const date = v => v ? `${v.slice(4,6)}/${v.slice(6,8)}` : '—';
  const ratio = (v,base) => v!=null && base>0 ? (v/base-1)*100 : null;
  const tag = r => `<span class="status-tag status-${esc(r.status)}">${r.status==='excluded'?'×':r.eligible?'●':'○'} ${esc(r.label)}</span>`;
  const grade = r => `<span class="grade grade-${esc(r.grade)}">${esc(r.grade)}</span>`;
  let data=null, holdings=null, selected=null, filter='all', cards=false, busy=false, received=0, auxiliaryAt=0, boardSignature='';
  let transportError='', marketError='', detailSignature='', detailView='price';
  const collapsed=new Set(['excluded','review']);
  const groups=[
    ['excluded','× 제외 · 퇴출','저가 이탈 또는 필수 조건 미충족'],
    ['position','▣ 보유 중','매수일 기준 D+2 · 익절 +15% 70% · 손절 −10%'],
    ['today','◷ D+2 자리','11시 미리보기 → 12시 최종 확인'],
    ['next','◐ 내일 후보','D+1 오후 횡보 · 다음 거래일 오전 저가 확인'],
    ['baseline','○ D+1 대기','기준봉 확인 · 다음 거래일 슈팅과 횡보 관찰'],
    ['review','◌ 지난 판정','진입 시각이 지난 자료 · 당시 조건 참고'],
    ['unknown','· 확인 필요','거래일과 누락 자료 확인']
  ];
  const positionFor = code => holdings?.enabled ? holdings.positions?.find(p=>p.code===code) : null;
  function groupFor(r){
    if(positionFor(r.code))return 'position';
    if(r.status==='excluded')return 'excluded';
    if(r.status==='review')return 'review';
    return r.day===2?'today':r.day===1?'next':r.day===0?'baseline':'unknown';
  }
  function visibleRows(){
    const q=$('#search').value.trim().toLowerCase();
    return (data?.rows||[]).filter(r=>(!q||`${r.name} ${r.code} ${r.theme}`.toLowerCase().includes(q)) &&
      (filter==='all'||filter==='excluded'&&r.status==='excluded'||filter==='today'&&r.day===2||filter==='watch'&&['baseline','watch','waiting','preview','incomplete'].includes(r.status)));
  }
  async function api(url,method='GET'){
    const r=await fetch(url,{method,cache:'no-store',signal:AbortSignal.timeout(20000)});
    if(!r.ok){let msg;try{msg=(await r.json()).detail}catch{};throw Error(r.status===404?'새 기능 적용을 위해 테마 레이더 서버를 다시 시작해 주세요.':msg||`조회 실패 (${r.status})`)}
    return r.json();
  }
  function banner(){const errors=[transportError,marketError,data?.error].filter(Boolean);$('#errorBanner').hidden=!errors.length;$('#errorBanner').textContent=errors.join(' · ')}
  function freshness(){
    const fresh=received && Date.now()-received<20000 && !transportError;
    $('#connectionLight').classList.toggle('good',!!fresh);
    $('#connection').textContent=fresh?(data?.job?.running?'수집 중':'서버 연결'):received?'연결 지연':'연결 확인 중';
    $('#connection').className=fresh?'good':'warn';
    $('#received').textContent=received?`화면 ${time(received)} · 시세 수집 ${time(data?.updatedAt||data?.scanAt)}`:'자료 수신 대기';
    $('#clock').textContent=new Date().toLocaleTimeString('ko-KR',{timeZone:'Asia/Seoul',hour12:false});
  }
  function render(){
    if(!data)return;
    const rows=data.rows||[], ready=rows.filter(r=>r.eligible), watch=rows.filter(r=>!['excluded','review'].includes(r.status)), next=watch.filter(r=>r.day===1), base=watch.filter(r=>r.day===0);
    $('#readyCount').innerHTML=ready.length?`${ready.length}<small>종목</small>`:'—';
    $('#readyCount').classList.toggle('has-ready',!!ready.length);
    $('#readyNote').textContent=ready.length?ready.map(r=>r.name).join(' · '):'현재 정오 조건을 충족한 자리가 없습니다';
    $('#watchCount').innerHTML=`${watch.length}<small>종목</small>`;
    $('#stageSummary').textContent=`내일 후보 ${next.length} · D+2 ${watch.filter(r=>r.day===2).length} · 기준봉 ${base.length}`;
    $('#todayReady').textContent=ready.length;$('#todayWatch').textContent=next.length;$('#todayBase').textContent=base.length;
    $('#todayAsOf').textContent=`기준 거래일 ${data.asof||'미확인'}${data.calendarConfirmed?'':' · 확인 필요'}`;
    $('#scope').textContent=data.scope||'검색 범위 확인 중';
    const j=data.job||{};$('#refreshButton').disabled=!!j.running;
    $('#collectionStatus').textContent=j.running?`수집 ${j.done}/${j.total} · ${j.current} · 이전 자료 표시 중`:`기준 거래일 ${data.asof||'미확인'} · 마지막 수집 ${time(data.updatedAt||data.scanAt)} · 가격은 조회 시점 기준`;
    renderBoard();renderDetail();banner();freshness();
  }
  function rowHTML(r){
    const m=r.metrics||{}, p=positionFor(r.code), supportChange=ratio(r.price,m.support);
    const label=p?`<span class="status-tag status-position">▣ 보유 ${p.days_held==null?'일자 확인':'D+'+p.days_held}</span>`:tag(r);
    const d1=`${percent(m.d1Change)} / ${percent(m.d1HighPct)} · ${m.afternoonRange==null?'—':number(m.afternoonRange,1)+'%'}`;
    const base=`${date(r.d0)} ${r.base.kind} ${number(r.base.amountEok)}억 ${number(r.base.multiple,1)}배`;
    return `<tr class="stock-row ${r.id===selected?'selected':''}" data-id="${esc(r.id)}" tabindex="0" aria-label="${esc(r.name)} 상세 보기" aria-selected="${r.id===selected}"><td title="${esc(r.reason)}">${label}</td><td title="${esc(r.gradeNote)}">${grade(r)}</td><td title="${esc(r.name)} ${esc(r.code)}"><strong>${esc(r.name)}</strong></td><td class="theme-cell" title="${esc(r.theme)}">${esc(r.theme)}</td><td title="${esc(base)}">${date(r.d0)} ${esc(r.base.kind)} <span>${number(r.base.amountEok)}억</span><small>${number(r.base.multiple,1)}배</small></td><td title="마감 / 고가: 기준봉 종가 대비 · 오후 변동폭 ${esc(d1)} / 기울기 ${number(m.afternoonSlope,3)}%p/봉">${esc(d1)}</td><td title="D+1 저가 ${number(m.support)} · 조회가의 지지선 대비 ${percent(supportChange)}">${number(m.support)}<small class="${sign(supportChange)}">${percent(supportChange)}</small></td><td class="${sign(r.change)}">${number(r.price)}<small class="${sign(r.change)}">${percent(r.change)}</small></td><td title="${esc(r.news?.title||'연결된 뉴스 없음')}">${r.news?.title?'테마 뉴스 ↗':'—'}</td><td class="reason-cell ${r.eligible?'pass':''}" title="${esc(r.reason)}">${esc(r.reason)}</td></tr>`;
  }
  function renderBoard(){
    const rows=visibleRows();
    if(!rows.some(r=>r.id===selected))selected=(rows.find(r=>positionFor(r.code))||rows.find(r=>r.eligible)||rows[0])?.id||null;
    $('#boardCount').textContent=data?.rows?.length||0;$('#empty').hidden=!!rows.length;
    const columns=window.matchMedia('(min-width:961px) and (max-width:1799px)').matches?7:10;
    const html=groups.map(([key,title,hint])=>{
      const rs=rows.filter(r=>groupFor(r)===key);if(!rs.length)return '';
      const closed=collapsed.has(key)&&!$('#search').value.trim();
      return `<tr class="group-row ${key}"><td colspan="${columns}"><button class="group-toggle" data-group="${key}" aria-expanded="${!closed}"><span class="chevron">${closed?'▶':'▼'}</span><b>${title}</b><small>${rs.length}종목</small><span class="group-hint">${hint}</span></button></td></tr>`+(closed?'':rs.map(rowHTML).join(''));
    }).join('');
    if(html!==boardSignature){
      const focused=document.activeElement?.closest('[data-id]')?.dataset.id;
      $('#stockRows').innerHTML=html;boardSignature=html;
      if(focused)Array.from($('#stockRows').querySelectorAll('[data-id]')).find(el=>el.dataset.id===focused)?.focus({preventScroll:true});
    }
    if(cards){
      const cardHtml=rows.map(r=>`<button class="stock-tile ${selected===r.id?'selected':''}" data-id="${esc(r.id)}">${tag(r)} <span style="float:right">${grade(r)}</span><h3>${esc(r.name)} <small style="display:inline">${esc(r.code)}</small></h3><p>${esc(r.theme)} · ${date(r.d0)} ${esc(r.base.kind)}</p><p>기준 ${number(r.base.amountEok)}억 · ${number(r.base.multiple,1)}배</p><small>${esc(r.reason)}</small></button>`).join('');
      if($('#cardView').innerHTML!==cardHtml)$('#cardView').innerHTML=cardHtml;
    }
    $('#tableView').hidden=cards||!rows.length;$('#cardView').hidden=!cards||!rows.length;
  }
  function chart(r){
    const bars=r.chart||[];
    if(!bars.length)return '<div class="empty" style="padding:28px 5px"><p>15분봉 수신 대기<br>자료 없는 차트는 그리지 않습니다.</p></div>';
    const W=330,H=164,pad=10,foot=18;
    const levels=bars.flatMap(b=>[b.low,b.high]);
    if(r.metrics.support)levels.push(r.metrics.support);
    const lo=Math.min(...levels),hi=Math.max(...levels),delta=hi-lo||hi*.01;
    const y=v=>pad+(hi-v)/delta*(H-pad-foot),dx=(W-2*pad)/bars.length;
    let svg=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(r.name)} D+1 D+2 15분봉과 D+1 저가 지지선"><title>${esc(r.name)} KRX 15분봉</title>`;
    for(let i=0;i<4;i++){const yy=pad+i*(H-pad-foot)/3;svg+=`<path d="M0 ${yy}H${W}" stroke="#283747" stroke-dasharray="2 4"/>`}
    bars.forEach((b,i)=>{const x=pad+(i+.5)*dx,col=b.close>=b.open?'#eb837e':'#77a9e6';svg+=`<path d="M${x} ${y(b.high)}V${y(b.low)}" stroke="${col}"/><rect x="${x-Math.max(1,dx*.32)}" y="${Math.min(y(b.open),y(b.close))}" width="${Math.max(2,dx*.64)}" height="${Math.max(1,Math.abs(y(b.open)-y(b.close)))}" fill="${col}"/>`;if(i===0||b.stamp.slice(0,8)!==bars[i-1].stamp.slice(0,8))svg+=`<text x="${Math.max(0,x-3)}" y="${H-2}" fill="#8198b1" font-size="9">${date(b.stamp)}</text>`});
    if(r.metrics.support){let yy=y(r.metrics.support);svg+=`<path d="M0 ${yy}H${W}" stroke="#72dfc4" stroke-dasharray="5 3"/><text x="${W-4}" y="${Math.max(11,yy-4)}" text-anchor="end" fill="#72dfc4" font-size="10">D+1 저가 ${number(r.metrics.support)}</text>`}
    return svg+'</svg>';
  }

  function priceLadder(r){
    const m=r.metrics,p=positionFor(r.code),average=p?.average,reference=r.price;
    const levels=[['profit','익절 +15% · 평단',average?average*1.15:null],['','D+1 슈팅 +15%',r.baseClose*1.15],['current','▶ 조회가',r.price],['','D+1 종가',m.d1Change==null?null:r.baseClose*(1+m.d1Change/100)],['','기준봉 종가',r.baseClose],['support','─ 선 · D+1 저가',m.support],['','D+2 오전 저가',m.morningLow],['loss','손절 −10% · 평단',average?average*.9:null]];
    return `<section class="panel price-panel"><h3 class="panel-title">가격 사다리 <span>조회가 대비</span></h3><div class="price-ladder">${levels.map(([cls,label,value])=>`<div class="price-level ${cls}"><span>${label}</span><b>${number(value)}</b><small>${percent(ratio(value,reference))}</small></div>`).join('')}</div><p class="price-note">${p?`보유 ${number(p.quantity)}주 · 평단 ${number(average)} · 비용 전 계산값`:'익절·손절은 보유 평단 연결 후 표시합니다.'}</p></section>`;
  }
  function checkSection(r,keys,title,cls){
    const checks=r.checks.filter(c=>keys.includes(c.key));
    if(!checks.length)return '';
    return `<section class="panel check-panel ${cls}"><h3 class="panel-title">${title}<span>${checks.filter(c=>c.state==='pass').length}/${checks.length} 통과</span></h3>${checks.map(c=>`<div class="check ${esc(c.state)}"><span>${c.state==='pass'?'✓':c.state==='fail'?'×':'·'}</span><div><b>${esc(c.title)}</b><small>${esc(c.detail)}</small></div></div>`).join('')}</section>`;
  }
  function renderDetail(){
    const r=data?.rows?.find(x=>x.id===selected);
    $('#detail').hidden=!r;$('#detailEmpty').hidden=!!r;
    if(!r){detailSignature='';$('#chartButton').disabled=true;window.HorizonFlow?.mount(null,null);return}
    $('#chartButton').disabled=false;
    const m=r.metrics,f=m.firstHourBounds,news=r.news,p=positionFor(r.code);
    const holdingTag=p?`<span class="status-tag status-position">▣ 보유 ${p.days_held==null?'일자 확인':'D+'+p.days_held}</span>`:'';
    let url='';try{const u=new URL(news?.url);if(['http:','https:'].includes(u.protocol))url=u.href}catch{}
    const html=`<div class="detail-top"><div class="stock-heading"><div><h2>${esc(r.name)}</h2><small>${esc(r.code)}</small></div><div class="heading-status">${grade(r)}${holdingTag||tag(r)}</div></div>
      <div class="detail-tabs" role="group" aria-label="종목 상세 정보 선택"><button data-detail-tab="price" aria-pressed="true" aria-controls="detailPrice">가격·차트</button><button data-detail-tab="checks" aria-pressed="false" aria-controls="detailChecks">조건판정</button><button data-detail-tab="flow" aria-pressed="false" aria-controls="detailFlow">수급</button></div></div>
      <section class="panel verdict ${p?'holding':esc(r.status)}"><h3>${holdingTag||tag(r)}</h3>${p?`<p>${number(p.quantity)}주 · 평단 ${number(p.average)}원 · 매수일 ${esc(p.bought_on||'확인 필요')}</p><p>신규 진입 판정: ${esc(r.label)} · ${esc(r.reason)}</p>`:`<p>${esc(r.reason)}</p>`}<small>${esc(r.theme)} · 기준봉 ${date(r.d0)} · ${r.day==null?'거래일 미확인':'기준봉 D+'+r.day}</small></section>
      <div class="condition-strip" role="img" aria-label="${r.checks.filter(c=>c.state==='pass').length}개 조건 통과, ${r.checks.filter(c=>c.state==='fail').length}개 제외">${r.checks.map(c=>`<span class="${esc(c.state)}" title="${esc(c.title+' · '+c.detail)}"></span>`).join('')}</div>
      <div class="metrics-row"><div class="metric"><small>▣ 기준 대금</small><b>${number(r.base.amountEok)}<small>억</small></b><em>${number(r.base.multiple,1)}배</em></div><div class="metric"><small>▤ 오후 폭</small><b>${number(m.afternoonRange,1)}<small>%</small></b><em>5% 이내</em></div><div class="metric"><small>⌁ 오전 폭</small><b>${number(m.morningRange,1)}<small>%</small></b><em>4% 이상</em></div><div class="metric"><small>⌖ 지지선</small><b>${number(m.support)}</b><em>D+1 저가</em></div></div>
      <div id="detailPrice" class="detail-content" data-detail-panel="price">
      ${priceLadder(r)}
      <section class="panel chart-box"><h3 class="panel-title">D+1 → D+2 <span>KRX · 15분봉</span></h3>${chart(r)}<p class="chart-caption">수신 ${time(r.fetchedAt)} · 조회가 ${number(r.price)} <span class="${sign(r.change)}">${percent(r.change)}</span></p></section>
      </div><div id="detailChecks" class="detail-content" data-detail-panel="checks" hidden>
      <p class="detail-purpose">관찰 조건의 충족 여부입니다. 실제 주문·감시 상태는 자동매도 화면에서 확인합니다.</p>
      ${checkSection(r,['exact','repeat','d0close'],'① 돈이 들어왔나 · 기준봉','base')}
      <section class="panel notes-panel"><h3 class="panel-title">② 재료 · 연결된 테마 뉴스</h3>${news?.title?`${url?`<a class="news-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(news.title)} ↗</a>`:`<p>${esc(news.title)}</p>`}<small>${esc(news.scope)} · ${esc(news.at||'시각 미확인')}</small>`:'<p>연결된 뉴스가 없습니다.</p>'}<p>개별 종목의 재료 확정은 별도 확인이 필요합니다.</p></section>
      ${checkSection(r,['shoot','flat'],'③ 횡보하고 있나 · D+1','flat')}
      ${checkSection(r,['support','range'],'④ 선을 지켰나 · D+2','support')}
      ${checkSection(r,['restriction','grade'],'⑤ 등급과 제외 조건','grade-check')}
      <section class="panel notes-panel"><h3 class="panel-title">움직임 참고 <span>등급·진입 필터 아님</span></h3><div class="kv"><span>등급 근거</span><b>${esc(r.gradeNote)}</b><span>시총 · 조회 시점</span><b>${number(m.capEok)}억</b><span>D+1 상대강도</span><b>${m.relativeStrength==null?'—':number(m.relativeStrength,2)+'%p'}</b><span>첫 1시간 / 평소 일대금</span><b>${f?number(f[0],2)+'~'+number(f[1],2)+'배':'—'}</b><span>HA 양봉 · 오전 / 오후</span><b>${number(m.haMorning)}% / ${number(m.haAfternoon)}%</b><span>이전 평균 일중폭</span><b>${number(m.avgRange250,2)}% · ${number(m.rangeSample)}일</b><span>기준봉 전 20일 / 60일</span><b>${percent(m.pre20)} / ${percent(m.pre60)}</b></div>${(r.warnings||[]).map(w=>`<p class="warn">${esc(w)}</p>`).join('')}</section>`;
    const contents=html+'</div><div id="detailFlow" class="detail-content" data-detail-panel="flow" hidden><p class="detail-purpose">선택한 종목의 참고 수급입니다. 수신 시각과 수집 범위를 함께 확인하세요.</p><div id="horizonFlowSlot"></div></div>';
    if(detailSignature!==contents){const changed=$('#detail').dataset.selected!==r.id;$('#detail').innerHTML=contents;$('#detail').dataset.selected=r.id;detailSignature=contents;if(changed)$('.detail-rail').scrollTop=0}
    showDetailView();
    window.HorizonFlow?.mount($('#horizonFlowSlot'),r);
    if($('#chartDialog').open)renderLargeChart();
  }
  function showDetailView(){
    document.querySelectorAll('[data-detail-tab]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.detailTab===detailView)));
    document.querySelectorAll('[data-detail-panel]').forEach(p=>p.hidden=p.dataset.detailPanel!==detailView);
  }
  $('#detail').addEventListener('click',e=>{
    const button=e.target.closest('[data-detail-tab]');if(!button)return;
    detailView=button.dataset.detailTab;showDetailView();$('.detail-rail').scrollTop=0;
  });
  function renderLargeChart(){const r=data?.rows?.find(x=>x.id===selected);$('#chartTitle').textContent=r?`${r.name} · ${r.code}`:'종목을 선택하세요';$('#largeChart').innerHTML=r?chart(r)+`<p class="chart-caption">D+1 → D+2 · KRX 15분봉 · 수신 ${time(r.fetchedAt)}</p>`:''}
  function renderMarket(m){
    $('#marketIndices').innerHTML=['kospi','kosdaq'].map((key,i)=>{const x=m[key]||{};return `<div class="panel market-card"><h2>${i?'코스닥':'코스피'}</h2><strong class="${sign(x.chg)}">${number(x.price,2)}</strong><span class="market-change ${sign(x.chg)}">${percent(x.chg)}</span><small>대금 ${number(x.amountJo,2)}조 · ${esc(x.at||'—')}</small><div class="market-stats">↑ <b class="up">${number(x.rise)}</b> · ↓ <b class="down">${number(x.fall)}</b></div>${(x.investors||[]).slice(0,6).map(([n,v])=>`<div class="flow-row"><span>${esc(n)}</span><b class="${sign(v)}">${v>0?'+':''}${number(v)}</b></div>`).join('')}</div>`}).join('');
    $('#marketTime').textContent=time(m.asof);
    $('#marketMore').innerHTML=`<span>선물</span><b class="${sign(m.futures?.chg)}">${number(m.futures?.price,2)}</b><span>KPI200</span><b>${number(m.kospi200?.price,2)}</b><span>베이시스</span><b>${number(m.basis,2)}</b><span>ADR 20일</span><b>${number(m.adr20,1)}%</b>`;
    $('#programFlows').innerHTML=['kospi','kosdaq'].map((key,i)=>`<span>${i?'코스닥':'코스피'}</span><b class="${sign(m[key]?.program)}">${number(m[key]?.program)}</b>`).join('');
    if(m.asof && Date.now()-new Date(m.asof).getTime()>180000)$('#marketTime').textContent+=' · 장외/지연';
  }
  function renderOvernight(o){
    const rs=new Map((o.rows||[]).map(x=>[x.key,x]));
    const sets=[['currency','환율 · 달러',['usdkrw','dxy']],['sentiment','변동성 · 외인 선물',['vix','frgn_fut']],['commodity','유가 · 원자재',['wti','brent','gold']],['global','해외 지수',['nasdaq','sox','twii']],['leaders wide','반도체 · 해외 종목',['smh','mu','tsm','kioxia','sndk','skhy_gdr']]];
    const labels={usdkrw:'환율',dxy:'DXY',frgn_fut:'외인',sox:'SOX',twii:'대만',nasdaq:'나스닥',brent:'브렌트',gold:'금',skhy_gdr:'SKHY GDR',tsm:'TSMC',mu:'마이크론'};
    $('#overnight').innerHTML=sets.map(([cls,title,keys])=>`<section class="panel mini-panel overnight-group ${cls}"><h2 class="panel-title">${title}</h2><div class="${cls.includes('leaders')?'leader-list':'kv'}">${keys.map(k=>{const x=rs.get(k)||{};const delta=x.diff!=null?`${x.diff>0?'+':''}${number(x.diff)}`:percent(x.chg);return cls.includes('leaders')?`<span title="${number(x.now,x.digits??2)}">${esc(labels[k]||x.name||k.toUpperCase())}<b class="${sign(x.chg)}">${delta}</b></span>`:`<span>${esc(labels[k]||x.name||k.toUpperCase())}</span><b class="${sign(x.chg??x.diff)}">${number(x.now,x.digits??2)}<small class="${sign(x.chg??x.diff)}">${delta}</small></b>`}).join('')}</div></section>`).join('');
    $('#overnightTime').textContent=`해외 수집 ${time(o.asof)} · 기준 ${o.base_at?new Date(o.base_at).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',hour12:false}):'미확인'}`;
  }
  function renderHoldings(){
    const h=holdings,ps=h?.positions||[];
    $('#holdingCount').innerHTML=h?.enabled?`${ps.length}<small>종목</small>`:'미연결';
    const listed=ps.filter(p=>data?.rows?.some(r=>r.code===p.code)).length;
    $('#holdingNote').textContent=h?.enabled?`계좌 조회 ${ps.length}종목 · 관찰 목록에 ${listed}종목`:'보유 버튼에서 잔고 조회 상태 확인';
    $('#holdingNote').title='계좌 보유 종목 수와 호라이즌 관찰 목록의 종목 수는 다를 수 있습니다. 실제 주문 감시는 자동매도 화면에서 확인합니다.';
    $('#positionsStatus').textContent=h?.enabled?'계좌 잔고 기반 · 실제 매도 주문은 전송하지 않습니다.':h?.error||'감시 미연결 · 아래 자료가 있으면 이전 검증의 저장값입니다.';
    $('#positions').innerHTML=ps.length?ps.map(p=>`<div class="position"><div><strong>${esc(p.name)} · ${number(p.quantity)}주</strong><p>평단 ${number(p.average)} · 매수일 ${esc(p.bought_on||'확인 필요')}</p></div><div><b>${p.days_held==null?'보유일 확인':`매수 D+${p.days_held}`}</b><p>익절 ${number(p.take_profit)} / 손절 ${number(p.stop_loss)}</p></div></div>`).join(''):'<div class="empty" style="padding:25px">연결된 보유 자료가 없습니다.</div>';
    const names={TAKE_PROFIT:'익절 도달',STOP_LOSS:'손절 도달',D2_CLOSE:'D+2 종가',D3_CLOSE:'D+3 종가 (이전 규칙)',OVERDUE_CLOSE:'청산일 경과'};
    $('#journal').innerHTML=h?.history?.length?[...h.history].reverse().map(s=>`<div class="journal-item"><div><b>${esc(s.name||s.code)} · ${number(s.quantity)}주</b><small>${esc(names[s.reason]||s.reason)} · 조건 신호</small></div><div>${esc(s.at?new Date(s.at).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',hour12:false}):'시각 미확인')}<small>참고가 ${number(s.reference_price)} · 주문 미전송</small></div></div>`).join(''):'<div class="empty"><h2>기록된 조건 신호가 없습니다</h2><p>매도조건 검증 중 익절·손절·D+2 조건이 충족되면 표시합니다.</p></div>';
  }
  async function auxiliary(){
    const result=await Promise.allSettled([api('/api/market'),api('/api/overnight'),api('/api/autosell')]);
    if(result[0].status==='fulfilled'){renderMarket(result[0].value);marketError=''}else marketError='시장 지표 조회 지연';
    if(result[1].status==='fulfilled')renderOvernight(result[1].value);
    holdings=result[2].status==='fulfilled'?result[2].value:null;
    renderHoldings();renderBoard();renderDetail();banner();
  }
  async function load(){
    if(busy||(document.hidden&&received))return;
    busy=true;
    try{data=await api('/api/horizon');received=Date.now();transportError='';render();if(Date.now()-auxiliaryAt>30000){auxiliaryAt=Date.now();await auxiliary()}}
    catch(e){transportError=e.message;banner();freshness()}
    finally{busy=false}
  }
  function select(event){
    const group=event.target.closest('[data-group]');if(group){const k=group.dataset.group;collapsed.has(k)?collapsed.delete(k):collapsed.add(k);renderBoard();$(`[data-group="${k}"]`)?.focus({preventScroll:true});return}
    const el=event.target.closest('[data-id]');if(!el)return;selected=el.dataset.id;renderBoard();renderDetail();
  }
  $('#stockRows').addEventListener('click',select);$('#cardView').addEventListener('click',select);
  $('#stockRows').addEventListener('keydown',e=>{
    if(e.target.closest('[data-group]'))return;
    if(e.key==='Enter'||e.key===' '){e.preventDefault();select(e)}
    if(e.key==='ArrowDown'||e.key==='ArrowUp'){
      const els=[...$('#stockRows').querySelectorAll('[data-id]')],i=els.indexOf(e.target.closest('[data-id]')),next=els[i+(e.key==='ArrowDown'?1:-1)];
      if(next){e.preventDefault();selected=next.dataset.id;renderBoard();renderDetail();const row=[...$('#stockRows').querySelectorAll('[data-id]')].find(el=>el.dataset.id===selected);row?.focus({preventScroll:true});row?.scrollIntoView({block:'nearest',inline:'nearest'})}
    }
  });
  document.querySelectorAll('[data-filter]').forEach(b=>b.addEventListener('click',()=>{filter=b.dataset.filter;if(filter==='excluded')collapsed.delete('excluded');document.querySelectorAll('[data-filter]').forEach(x=>x.classList.toggle('active',x===b));renderBoard();renderDetail()}));
  $('#search').addEventListener('input',()=>{renderBoard();renderDetail()});
  $('#viewButton').addEventListener('click',()=>{cards=!cards;$('#viewButton').textContent=cards?'☷ 표로':'▦ 카드로';$('#viewButton').setAttribute('aria-pressed',String(cards));renderBoard()});
  $('#rulesButton').addEventListener('click',()=>{$('#ruleList').innerHTML=(data?.rules||[]).map(r=>`<article class="rule"><span class="category">${esc(r.category)}</span><strong>${esc(r.title)}</strong><p>${esc(r.text)}</p></article>`).join('')||'<p>서버에 연결한 뒤 규칙을 확인할 수 있습니다.</p>';$('#rulesDialog').showModal()});
  $('#resultsButton').addEventListener('click',()=>$('#resultsDialog').showModal());
  for(const id of ['#holdingsButton','#holdingSummary'])$(id).addEventListener('click',()=>{renderHoldings();$('#holdingsDialog').showModal()});
  $('#journalButton').addEventListener('click',()=>{renderHoldings();$('#journalDialog').showModal()});
  $('#chartButton').addEventListener('click',()=>{renderLargeChart();$('#chartDialog').showModal()});
  document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>b.closest('dialog').close()));
  $('#refreshButton').addEventListener('click',async()=>{const b=$('#refreshButton');b.disabled=true;try{await api('/api/horizon/refresh','POST');await load()}catch(e){transportError=e.message;banner()}finally{b.disabled=!!data?.job?.running}});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)load()});window.addEventListener('online',load);window.addEventListener('resize',()=>{if(data)renderBoard()});
  load();setInterval(load,5000);setInterval(freshness,1000);
})();
