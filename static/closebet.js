(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = v => Number(v || 0).toLocaleString('ko-KR');
  const names = {submitting:'전송 중', accepted:'접수 · 체결 대기', partial:'일부 체결', filled:'전량 체결', closed:'잔량 종료', unknown:'응답 불명',
    attention:'주문 확인 필요', not_sent:'미전송', rejected:'거부', dry:'흉내', held:'보유 중', selling:'매도 주문 중', sold:'매도 완료'};
  const condNames = {amount:'대금', macd:'MACD', stoch:'스토', stoch240:'240분', prev:'전날', high:'고가', data:'자료', error:'조회'};
  let token = '', state = null, busy = false, polling = false, preview = null;
  const message = (id, text) => { $(id).textContent = text; $(id).hidden = !text; };
  async function api(path = '', body) {
    const res = await fetch('/api/closebet' + path, {method: body === undefined ? 'GET' : 'POST', cache: 'no-store', signal: AbortSignal.timeout(180000),
      headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-Closebet-Token': token}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
    let data; try { data = await res.json(); } catch { throw new Error('서버 응답을 확인할 수 없습니다.'); }
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '입력값 또는 서버 연결을 확인하세요.');
    return data;
  }
  function fillForm(s) {
    if (document.activeElement && $('settingsForm').contains(document.activeElement)) return;
    $('modeDry').checked = s.mode === 'dry'; $('modeLive').checked = s.mode === 'live';
    $('enabled').checked = s.enabled; $('perTrade').value = s.per_trade_amount; $('totalCap').value = s.total_cap;
    $('maxPositions').value = s.max_positions; $('threshold').value = s.threshold_eok; $('sellTime').value = s.sell_time;
    modeChanged();
  }
  function modeChanged() {
    const live = $('modeLive').checked;
    $('liveConfirm').hidden = !live; $('modeLiveLabel').classList.toggle('on', live); $('modeDryLabel').classList.toggle('on', !live);
    if (!live) $('confirmLive').checked = false;
  }
  function conds(checks) {
    return Object.entries(checks || {}).map(([k, [ok, why]]) => `<span class="cond ${ok ? 'ok' : ''}" title="${esc(why)}">${ok ? '✓' : '✗'} ${esc(condNames[k] || k)}</span>`).join('');
  }
  function renderScan(scan, label) {
    if (!scan) { $('scanMeta').textContent = '아직 후보 선정 전 (매 거래일 15:15 자동)'; $('scanRows').innerHTML = '<tr><td colspan="4" class="empty">아직 후보 선정 전</td></tr>'; return; }
    const rows = scan.rows || [], passed = rows.filter(r => r.pass);
    $('scanMeta').textContent = `${label} ${scan.at.slice(5, 16).replace('T', ' ')} · 문턱 ${fmt(scan.threshold_eok)}억 넘는 ${rows.length}종목 중 통과 ${passed.length}${scan.took_ms ? ' · ' + (scan.took_ms / 1000).toFixed(0) + '초' : ''}${scan.error ? ' · ' + scan.error : ''}`;
    $('scanRows').innerHTML = rows.length ? rows.map(r => `<tr class="${r.pass ? 'pass' : ''}"><td><b>${esc(r.name)}</b> <small>${esc(r.code)}</small>${r.pass ? ' <b style="color:#9fe3b3">통과</b>' : ''}</td><td class="num">${fmt(Math.round(r.amount_eok))}억</td><td class="num">${Number(r.chg).toFixed(2)}%</td><td>${conds(r.checks)}</td></tr>`).join('')
      : `<tr><td colspan="4" class="empty">${scan.error || '문턱을 넘는 종목이 없습니다'}</td></tr>`;
  }
  function render(s) {
    state = s; if (s.token) token = s.token;
    const live = s.live;
    $('status').textContent = s.error ? '상태 확인 필요' : !s.enabled ? '자동매매 꺼짐' : live ? '실전 자동매매 켜짐' : '흉내 모드 켜짐';
    $('statusDot').classList.toggle('active', Boolean(s.enabled && !s.error));
    $('environment').textContent = (s.environment === 'real' ? '실전 계좌' : '모의 계좌') + (live ? ' · 실제 전송' : ' · 전송 안 함');
    $('connection').textContent = s.connected ? '계좌 감시 연결됨' : '계좌 감시 연결 전 (실전 주문 불가)';
    $('updated').textContent = '조회 ' + s.asof.slice(11, 19);
    $('headline').textContent = s.enabled ? (live ? '실전 ON' : '흉내 ON') : 'OFF';
    const d = s.day || {};
    $('headlineNote').textContent = d.stopped ? d.stopped : s.scanning ? '후보 선정 중…' : d.bought ? `오늘 매수 단계 ${d.bought.slice(11, 19)} 진행` : d.scanned ? `후보 선정 ${d.scanned.slice(11, 16)} 완료` : `다음 단계: 15:15 후보 선정 → 15:19:50 매수 → ${s.settings.sell_time} 매도`;
    $('timeline').innerHTML = [['scanned', '15:15 후보'], ['bought', '15:19:50 매수'], ['filled', '15:31 체결 확인'], ['sold', s.settings.sell_time + ' 매도'], ['retried', '09:03 대조·재시도']]
      .map(([k, t]) => `<span class="${d[k] ? 'done' : ''}">${d[k] ? '✓ ' : ''}${esc(t)}</span>`).join('');
    ['save', 'stopAll', 'preview', 'reconcile'].forEach(id => $(id).disabled = busy || !token);
    message('error', s.error || '');
    fillForm(s.settings);
    if (!preview) renderScan(s.scan, '15:15 선정');
    $('skipped').textContent = (d.skipped || []).length ? '건너뜀: ' + d.skipped.join(' · ') : '';
    $('invested').textContent = fmt(s.invested); $('capShown').textContent = fmt(s.settings.total_cap);
    const pos = s.positions.filter(p => p.status !== 'sold').concat(s.positions.filter(p => p.status === 'sold').slice(-10));
    $('positions').innerHTML = pos.length ? pos.map(p => `<tr><td><b>${esc(p.name)}</b> <small>${esc(p.code)}</small>${p.dry ? ' <small>(흉내)</small>' : ''}</td><td class="num">${fmt(p.quantity)}</td><td class="num">${fmt(Math.round(p.average))}</td><td>${esc(p.bought_on)}</td><td><span class="pill ${esc(p.status)}">${esc(names[p.status] || p.status)}</span></td><td>${esc(p.note || (p.status === 'held' ? `다음 거래일 ${s.settings.sell_time} 시장가 매도` : ''))}</td></tr>`).join('')
      : '<tr><td colspan="6" class="empty">보유 없음</td></tr>';
    $('orders').innerHTML = s.orders.length ? [...s.orders].reverse().map(o => `<tr><td>${esc(o.at.slice(5, 19).replace('T', ' '))}</td><td><b>${esc(o.name)}</b> <small>${esc(o.code)}</small></td><td>${o.side === 'buy' ? '매수' : '매도'}${o.dry ? ' (흉내)' : ''}<br><small>${esc(o.reason)}</small></td><td class="num">${fmt(o.filled)} / ${fmt(o.quantity)}</td><td class="num">${o.limit_price ? fmt(o.limit_price) : '시장가'}</td><td><span class="pill ${esc(o.status)}">${esc(names[o.status] || o.status)}</span>${o.order_no ? `<br><small>주문 ${esc(o.order_no)}</small>` : ''}</td><td>${esc(o.note)}</td></tr>`).join('')
      : '<tr><td colspan="7" class="empty">주문 기록 없음</td></tr>';
  }
  async function refresh() {
    if (polling || busy) return; polling = true;
    try { render(await api()); } catch (e) { $('status').textContent = '연결 확인 필요'; $('statusDot').classList.remove('active'); message('error', e.message); }
    finally { polling = false; }
  }
  async function act(fn) {
    busy = true; ['save', 'stopAll', 'preview', 'reconcile'].forEach(id => $(id).disabled = true);
    try { await fn(); message('error', ''); } catch (e) { message('error', e.message); }
    finally { busy = false; await refresh(); }
  }
  $('modeDry').addEventListener('change', modeChanged); $('modeLive').addEventListener('change', modeChanged);
  $('settingsForm').addEventListener('submit', ev => { ev.preventDefault(); act(async () => {
    const body = {mode: $('modeLive').checked ? 'live' : 'dry', enabled: $('enabled').checked, per_trade_amount: Number($('perTrade').value),
      max_positions: Number($('maxPositions').value), total_cap: Number($('totalCap').value), threshold_eok: Number($('threshold').value),
      sell_time: $('sellTime').value, confirm_live: $('confirmLive').checked};
    render(await api('/settings', body)); $('saveNote').textContent = '저장됨 ' + new Date().toLocaleTimeString('ko-KR');
  }); });
  $('stopAll').addEventListener('click', () => { if (confirm('비상 정지: 새 주문 전송을 모두 막습니다. 이미 접수된 주문은 키움에서 직접 취소하세요.')) act(async () => render(await api('/stop', {}))); });
  $('preview').addEventListener('click', () => act(async () => { $('scanMeta').textContent = '후보를 보는 중… (1~2분)'; preview = await api('/scan', {}); renderScan(preview, '미리보기'); setTimeout(() => { preview = null; }, 600000); }));
  $('reconcile').addEventListener('click', () => act(async () => render(await api('/reconcile', {}))));
  refresh(); setInterval(refresh, 5000);
})();
