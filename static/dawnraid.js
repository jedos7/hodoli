(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = v => Number(v || 0).toLocaleString('ko-KR');
  const names = {submitting:'전송 중', accepted:'접수 · 체결 대기', partial:'일부 체결', filled:'전량 체결', closed:'잔량 종료',
    unknown:'응답 불명', attention:'주문 확인 필요', not_sent:'미전송', rejected:'거부', dry:'흉내',
    held:'보유 중', selling:'익절 주문 중', sold:'정리 완료'};
  const what = {DAWN_RAID: '09:00 매수', TAKE_PROFIT: '+7% 익절', RAID_CLOSE: '10:00 정리'};
  let token = '', busy = false, polling = false, preview = null;
  const message = (id, text) => { $(id).textContent = text; $(id).hidden = !text; };
  async function api(path = '', body) {
    const res = await fetch('/api/dawnraid' + path, {method: body === undefined ? 'GET' : 'POST', cache: 'no-store',
      signal: AbortSignal.timeout(180000),
      headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-Dawnraid-Token': token},
      ...(body === undefined ? {} : {body: JSON.stringify(body)})});
    let data; try { data = await res.json(); } catch { throw new Error('서버 응답을 확인할 수 없습니다.'); }
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '입력값 또는 서버 연결을 확인하세요.');
    return data;
  }
  function fillForm(s) {
    if (document.activeElement && $('settingsForm').contains(document.activeElement)) return;
    $('modeDry').checked = s.mode === 'dry'; $('modeLive').checked = s.mode === 'live';
    $('enabled').checked = s.enabled; $('slotAmount').value = s.slot_amount;
    $('totalCap').value = s.total_cap; $('maxPositions').value = s.max_positions;
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
      const deep = r.gap != null && r.gap <= -7;
      const gap = r.gap == null ? '—' : `${r.gap > 0 ? '+' : ''}${r.gap.toFixed(2)}%`;
      return `<tr class="${deep ? 'pass' : ''}"><td><b>${esc(r.name)}</b> <small>${esc(r.code)}</small></td>`
        + `<td class="num">${r.prev_chg.toFixed(2)}%</td><td class="num">${fmt(Math.round(r.prev_amount_eok))}억</td>`
        + `<td class="num">${fmt(r.prev_close)}</td><td class="num">${r.expected ? fmt(r.expected) : '—'}</td>`
        + `<td class="num">${gap}${deep ? ' <b style="color:#9fe3b3">후보</b>' : ''}</td></tr>`;
    }).join('') : '<tr><td colspan="6" class="empty">후보 없음</td></tr>';
  }
  function render(s) {
    if (window.renderBreakers) renderBreakers(s.breakers);
    if (s.token) token = s.token;
    const live = s.live;
    $('status').textContent = s.error ? '상태 확인 필요' : !s.enabled ? '자동매매 꺼짐' : live ? '실전 자동매매 켜짐' : '흉내 모드 켜짐';
    $('statusDot').classList.toggle('active', Boolean(s.enabled && !s.error));
    $('environment').textContent = (s.environment === 'real' ? '실전 계좌' : '모의 계좌') + (live ? ' · 실제 전송' : ' · 전송 안 함');
    $('connection').textContent = s.connected ? '계좌 감시 연결됨' : '계좌 감시 연결 전 (실전 주문 불가)';
    $('updated').textContent = '조회 ' + s.asof.slice(11, 19);
    $('headline').textContent = s.enabled ? (live ? '실전 ON' : '흉내 ON') : 'OFF';
    const d = s.day || {};
    const picked = d.picked || [];
    $('headlineNote').textContent = d.stopped || d.error || d.list_error
      || (d.closed ? '10:00 정리까지 끝' : d.bought ? (picked.length ? `08:59:45 ${picked.map(p => p.name).join(', ')}` : '오늘은 살 종목 없음')
        : d.listed ? `후보 ${d.candidates || 0}종목 · 08:59:45에 예상체결로 고릅니다` : '다음 단계: 08:10 후보 선정');
    $('timeline').innerHTML = [['08:10 후보', Boolean(d.listed)], ['08:59:45 판단', Boolean(d.bought)],
      ['09:00:30 체결', Boolean(d.filled)], ['09:59:50 익절 거둠', Boolean(d.cancelled)], ['10:00 정리', Boolean(d.closed)]]
      .map(([t, done]) => `<span class="${done ? 'done' : ''}">${done ? '✓ ' : ''}${esc(t)}</span>`).join('');
    ['save', 'stopAll', 'preview', 'reconcile'].forEach(id => $(id).disabled = busy || !token);
    message('error', s.error || d.connect_error || d.list_error || '');
    fillForm(s.settings);
    if (!preview) {
      const gap = d.index_gap == null ? '' : ` · 코스피 예상 갭 ${d.index_gap > 0 ? '+' : ''}${d.index_gap}%`;
      renderWatch(s.watch, s.watch && s.watch.length
        ? `후보 ${s.watch.length}종목${d.based_on ? ' · ' + d.based_on + ' 자료' : ''}${gap}`
        : '자동매매를 켜면 08:10에 전날 자료로 후보를 고릅니다. "지금 후보 보기"로 바로 볼 수도 있습니다.');
    }
    $('skipped').textContent = (d.skipped || []).length ? '건너뜀: ' + d.skipped.join(' · ') : '';
    $('invested').textContent = fmt(s.invested); $('capShown').textContent = fmt(s.settings.total_cap);
    const pos = s.positions.filter(p => p.status !== 'sold').concat(s.positions.filter(p => p.status === 'sold').slice(-10));
    $('positions').innerHTML = pos.length ? pos.map(p => `<tr><td><b>${esc(p.name)}</b> <small>${esc(p.code)}</small>${p.dry ? ' <small>(흉내)</small>' : ''}</td>`
      + `<td class="num">${fmt(p.quantity)}</td><td class="num">${fmt(Math.round(p.average))}</td>`
      + `<td class="num">${p.take_profit ? fmt(p.take_profit) : '—'}</td>`
      + `<td><span class="pill ${esc(p.status)}">${esc(names[p.status] || p.status)}</span></td><td>${esc(p.note || '')}</td></tr>`).join('')
      : '<tr><td colspan="6" class="empty">보유 없음</td></tr>';
    $('orders').innerHTML = s.orders.length ? [...s.orders].reverse().map(o => `<tr><td>${esc(o.at.slice(5, 19).replace('T', ' '))}</td>`
      + `<td><b>${esc(o.name)}</b> <small>${esc(o.code)}</small></td><td>${esc(what[o.reason] || o.reason || '')}${o.dry ? ' (흉내)' : ''}</td>`
      + `<td class="num">${fmt(o.filled)} / ${fmt(o.quantity)}</td><td class="num">${fmt(o.limit_price)}</td>`
      + `<td><span class="pill ${esc(o.status)}">${esc(names[o.status] || o.status)}</span>${o.order_no ? `<br><small>주문 ${esc(o.order_no)}</small>` : ''}</td><td>${esc(o.note)}</td></tr>`).join('')
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
      total_cap: Number($('totalCap').value), confirm_live: $('confirmLive').checked}));
    $('saveNote').textContent = '저장됨 ' + new Date().toLocaleTimeString('ko-KR');
  }); });
  $('stopAll').addEventListener('click', () => { if (confirm('비상 정지: 새 매수를 막습니다. 익절 취소와 10:00 정리는 그대로 나갑니다.')) act(async () => render(await api('/stop', {}))); });
  $('preview').addEventListener('click', () => act(async () => {
    $('watchMeta').textContent = '후보를 보는 중… (전 종목을 훑습니다)';
    preview = await api('/scan', {});
    renderWatch(preview.rows, `미리보기 ${preview.at.slice(5, 16).replace('T', ' ')} · ${preview.rows.length}종목`
      + (preview.based_on ? ` · ${preview.based_on} 자료` : ''));
    setTimeout(() => { preview = null; }, 300000);
  }));
  $('reconcile').addEventListener('click', () => act(async () => render(await api('/reconcile', {}))));
  refresh(); setInterval(refresh, 5000);
})();
