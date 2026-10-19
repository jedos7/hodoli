(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = v => Number(v || 0).toLocaleString('ko-KR');
  const names = {submitting:'전송 중', accepted:'접수 · 체결 대기', partial:'일부 체결', filled:'전량 체결', closed:'잔량 종료',
    unknown:'응답 불명', attention:'주문 확인 필요', not_sent:'미전송', rejected:'거부', dry:'흉내',
    held:'보유 중', selling:'매도 주문 중', sold:'매도 완료'};
  let token = '', busy = false, polling = false, preview = null;
  const message = (id, text) => { $(id).textContent = text; $(id).hidden = !text; };
  async function api(path = '', body) {
    const res = await fetch('/api/leaderdip' + path, {method: body === undefined ? 'GET' : 'POST', cache: 'no-store',
      signal: AbortSignal.timeout(120000),
      headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-Leaderdip-Token': token},
      ...(body === undefined ? {} : {body: JSON.stringify(body)})});
    let data; try { data = await res.json(); } catch { throw new Error('서버 응답을 확인할 수 없습니다.'); }
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '입력값 또는 서버 연결을 확인하세요.');
    return data;
  }
  function fillForm(s) {
    if (document.activeElement && $('settingsForm').contains(document.activeElement)) return;
    $('modeDry').checked = s.mode === 'dry'; $('modeLive').checked = s.mode === 'live';
    $('enabled').checked = s.enabled; $('slotAmount').value = s.slot_amount; $('totalCap').value = s.total_cap;
    $('maxPositions').value = s.max_positions; $('topN').value = s.top_n; $('sellTime').value = s.sell_time;
    modeChanged();
  }
  function modeChanged() {
    const live = $('modeLive').checked;
    $('liveConfirm').hidden = !live; $('modeLiveLabel').classList.toggle('on', live); $('modeDryLabel').classList.toggle('on', !live);
    if (!live) $('confirmLive').checked = false;
  }
  function renderWatch(rows, label) {
    $('watchMeta').textContent = label;
    $('watchRows').innerHTML = rows && rows.length ? rows.map(r => {
      const touched = r.last != null && r.f2 && r.last < r.f2;
      const gap = r.gap == null ? '—' : `${r.gap > 0 ? '+' : ''}${r.gap.toFixed(2)}%`;
      return `<tr class="${touched ? 'touched' : ''}"><td><b>${esc(r.name)}</b> <small>${esc(r.code)}</small>${r.note ? `<br><small>${esc(r.note)}</small>` : ''}</td><td class="num zone">${r.anchor ? fmt(Math.round(r.anchor)) : '—'}</td><td class="num zone"><b>${r.f2 ? fmt(r.f2) : '—'}</b></td><td class="num zone">${r.f3 ? fmt(r.f3) : '—'}</td><td class="num">${r.last ? fmt(Math.round(r.last)) : '—'}</td><td class="num">${gap}${touched ? ' <b style="color:#9fe3b3">터치</b>' : ''}</td></tr>`;
    }).join('') : '<tr><td colspan="6" class="empty">감시 자료 없음</td></tr>';
  }
  function render(s) {
    if (s.token) token = s.token;
    const live = s.live;
    $('status').textContent = s.error ? '상태 확인 필요' : !s.enabled ? '자동매매 꺼짐' : live ? '실전 자동매매 켜짐' : '흉내 모드 켜짐';
    $('statusDot').classList.toggle('active', Boolean(s.enabled && !s.error));
    $('environment').textContent = (s.environment === 'real' ? '실전 계좌' : '모의 계좌') + (live ? ' · 실제 전송' : ' · 전송 안 함');
    $('connection').textContent = s.connected ? '계좌 감시 연결됨' : '계좌 감시 연결 전 (실전 주문 불가)';
    $('updated').textContent = '조회 ' + s.asof.slice(11, 19);
    $('headline').textContent = s.enabled ? (live ? '실전 ON' : '흉내 ON') : 'OFF';
    const d = s.day || {};
    const setups = s.setups || [];
    $('headlineNote').textContent = d.stopped || d.error
      || (setups.length ? `오늘 ${setups.length}자리 걸림` : s.polledAt ? `감시 중 · 마지막 확인 ${s.polledAt.slice(11, 19)}` : '10:00~14:59에 F2를 지켜봅니다');
    $('timeline').innerHTML = [['polled', '10:00~14:59 감시', Boolean(s.polledAt)], ['touched', 'F2 터치', setups.length > 0],
      ['cancelled', '15:19 미체결 취소', Boolean(d.cancelled)], ['filled', '15:31 체결 확인', Boolean(d.filled)],
      ['sold', (s.settings.sell_time || '08:40') + ' 매도', Boolean(d.sold)]]
      .map(([, t, done]) => `<span class="${done ? 'done' : ''}">${done ? '✓ ' : ''}${esc(t)}</span>`).join('');
    ['save', 'stopAll', 'preview', 'reconcile'].forEach(id => $(id).disabled = busy || !token);
    message('error', s.error || d.connect_error || '');
    fillForm(s.settings);
    if (!preview) renderWatch(s.watch, s.watch && s.watch.length
      ? `대장주 ${s.watch.length}종목 · ${s.polledAt ? '마지막 확인 ' + s.polledAt.slice(11, 19) : '감시 전'}`
      : '자동매매를 켜면 1분마다 F존을 갱신합니다. "지금 F존 보기"로 바로 볼 수도 있습니다.');
    $('setupRows').innerHTML = setups.length ? setups.map(x => `<tr><td>${esc(x.at.slice(11, 19))}</td><td><b>${esc(x.name)}</b> <small>${esc(x.code)}</small></td><td class="num zone">${fmt(Math.round(x.anchor))}</td><td class="num zone"><b>${fmt(x.f2)}</b></td><td class="num zone">${fmt(x.f3)}</td><td>${x.crowded ? '몰린 날 · 반 칸 · ' : ''}${esc(x.note || '')}</td></tr>`).join('')
      : '<tr><td colspan="6" class="empty">아직 F2에 닿은 대장주가 없습니다</td></tr>';
    $('skipped').textContent = (d.skipped || []).length ? '건너뜀: ' + d.skipped.join(' · ') : '';
    $('invested').textContent = fmt(s.invested); $('capShown').textContent = fmt(s.settings.total_cap);
    const pos = s.positions.filter(p => p.status !== 'sold').concat(s.positions.filter(p => p.status === 'sold').slice(-10));
    $('positions').innerHTML = pos.length ? pos.map(p => `<tr><td><b>${esc(p.name)}</b> <small>${esc(p.code)}</small>${p.dry ? ' <small>(흉내)</small>' : ''}</td><td class="num">${fmt(p.quantity)}</td><td class="num">${fmt(Math.round(p.average))}</td><td>${esc((p.zones || []).join('·') || '—')}</td><td>${esc(p.bought_on)}</td><td><span class="pill ${esc(p.status)}">${esc(names[p.status] || p.status)}</span></td><td>${esc(p.note || (p.status === 'held' ? `다음 거래일 ${s.settings.sell_time} 시장가 매도` : ''))}</td></tr>`).join('')
      : '<tr><td colspan="7" class="empty">보유 없음</td></tr>';
    $('orders').innerHTML = s.orders.length ? [...s.orders].reverse().map(o => `<tr><td>${esc(o.at.slice(5, 19).replace('T', ' '))}</td><td><b>${esc(o.name)}</b> <small>${esc(o.code)}</small></td><td>${esc(o.zone || '')}${o.dry ? ' (흉내)' : ''}</td><td class="num">${fmt(o.filled)} / ${fmt(o.quantity)}</td><td class="num">${fmt(o.limit_price)}</td><td><span class="pill ${esc(o.status)}">${esc(names[o.status] || o.status)}</span>${o.order_no ? `<br><small>주문 ${esc(o.order_no)}</small>` : ''}</td><td>${esc(o.note)}</td></tr>`).join('')
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
    render(await api('/settings', {mode: $('modeLive').checked ? 'live' : 'dry', enabled: $('enabled').checked,
      slot_amount: Number($('slotAmount').value), max_positions: Number($('maxPositions').value),
      total_cap: Number($('totalCap').value), top_n: Number($('topN').value), sell_time: $('sellTime').value,
      confirm_live: $('confirmLive').checked}));
    $('saveNote').textContent = '저장됨 ' + new Date().toLocaleTimeString('ko-KR');
  }); });
  $('stopAll').addEventListener('click', () => { if (confirm('비상 정지: 새 주문을 막습니다. 이미 접수된 F2·F3 주문은 키움에서 직접 취소하세요.')) act(async () => render(await api('/stop', {}))); });
  $('preview').addEventListener('click', () => act(async () => {
    $('watchMeta').textContent = '대장주 F존을 보는 중…';
    preview = await api('/scan', {});
    renderWatch(preview.rows, `미리보기 ${preview.at.slice(5, 16).replace('T', ' ')} · 대장주 ${preview.rows.length}종목`);
    setTimeout(() => { preview = null; }, 300000);
  }));
  $('reconcile').addEventListener('click', () => act(async () => render(await api('/reconcile', {}))));
  refresh(); setInterval(refresh, 5000);
})();
