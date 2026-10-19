(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = v => Number(v || 0).toLocaleString('ko-KR');
  const names = {submitting:'전송 중', accepted:'접수 · 체결 대기', partial:'일부 체결', filled:'전량 체결', closed:'잔량 종료',
    unknown:'응답 불명', attention:'주문 확인 필요', not_sent:'미전송', rejected:'거부', dry:'흉내',
    held:'보유 중', linked:'자동매도 연결됨', sold:'매도 완료'};
  let token = '', busy = false, polling = false, preview = null;
  const message = (id, text) => { $(id).textContent = text; $(id).hidden = !text; };
  async function api(path = '', body) {
    const res = await fetch('/api/horizonbuy' + path, {method: body === undefined ? 'GET' : 'POST', cache: 'no-store',
      signal: AbortSignal.timeout(120000),
      headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-Horizonbuy-Token': token},
      ...(body === undefined ? {} : {body: JSON.stringify(body)})});
    let data; try { data = await res.json(); } catch { throw new Error('서버 응답을 확인할 수 없습니다.'); }
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '입력값 또는 서버 연결을 확인하세요.');
    return data;
  }
  function fillForm(s) {
    if (document.activeElement && $('settingsForm').contains(document.activeElement)) return;
    $('modeDry').checked = s.mode === 'dry'; $('modeLive').checked = s.mode === 'live';
    $('enabled').checked = s.enabled; $('linkExit').checked = s.link_exit;
    $('slotAmount').value = s.slot_amount; $('totalCap').value = s.total_cap;
    $('maxPositions').value = s.max_positions; $('minBase').value = s.min_base_eok;
    modeChanged();
  }
  function modeChanged() {
    const live = $('modeLive').checked;
    $('liveConfirm').hidden = !live; $('modeLiveLabel').classList.toggle('on', live); $('modeDryLabel').classList.toggle('on', !live);
    if (!live) $('confirmLive').checked = false;
  }
  function renderPicks(picks, skipped, label) {
    $('pickMeta').textContent = label;
    $('pickRows').innerHTML = picks && picks.length ? picks.map(p => `<tr><td><b>${esc(p.name)}</b> <small>${esc(p.code)}</small>${p.d0 ? `<br><small>기준봉 ${esc(p.d0)}</small>` : ''}</td><td><span class="grade ${esc(p.grade)}">${esc(p.grade)}</span></td><td class="num">${fmt(Math.round(p.base_eok))}억</td><td class="num">${fmt(p.price)}</td><td class="num">${p.slots}자리</td></tr>`).join('')
      : '<tr><td colspan="5" class="empty">조건을 넘은 자리가 없습니다</td></tr>';
    $('skipped').textContent = (skipped || []).length ? '걸러짐: ' + skipped.join(' · ') : '';
  }
  function render(s) {
    if (window.renderBreakers) renderBreakers(s.breakers);
    if (s.token) token = s.token;
    const live = s.live;
    $('status').textContent = s.error ? '상태 확인 필요' : !s.enabled ? '자동매수 꺼짐' : live ? '실전 자동매수 켜짐' : '흉내 모드 켜짐';
    $('statusDot').classList.toggle('active', Boolean(s.enabled && !s.error));
    $('environment').textContent = (s.environment === 'real' ? '실전 계좌' : '모의 계좌') + (live ? ' · 실제 전송' : ' · 전송 안 함');
    $('connection').textContent = s.connected ? '계좌 감시 연결됨' : '계좌 감시 연결 전 (실전 주문 불가)';
    $('updated').textContent = '조회 ' + s.asof.slice(11, 19);
    $('headline').textContent = s.enabled ? (live ? '실전 ON' : '흉내 ON') : 'OFF';
    const d = s.day || {};
    $('headlineNote').textContent = d.stopped || d.error || (d.settled ? '오늘 체결 확인까지 끝' : d.bought ? `12:00 매수 단계 ${d.bought.slice(11, 19)} 진행` : '다음 단계: 12:00 정각 판정 → 매수 → 12:05 체결·자동매도 연결');
    $('timeline').innerHTML = [['bought', '12:00 정오 매수'], ['settled', '12:05 체결·연결']]
      .map(([k, t]) => `<span class="${d[k] ? 'done' : ''}">${d[k] ? '✓ ' : ''}${esc(t)}</span>`).join('');
    ['save', 'stopAll', 'preview', 'reconcile'].forEach(id => $(id).disabled = busy || !token);
    message('error', s.error || d.connect_error || '');
    fillForm(s.settings);
    if (!preview) {
      const lp = s.lastPick || {};
      renderPicks(lp.picks, lp.skipped || d.skipped,
        lp.at ? `12:00 판정 ${lp.at.slice(5, 16).replace('T', ' ')} · 통과 ${(lp.picks || []).length}자리` : '오늘 12:00 판정 전 (또는 자동매수 꺼짐)');
    }
    $('invested').textContent = fmt(s.invested); $('capShown').textContent = fmt(s.settings.total_cap);
    const pos = s.positions.filter(p => p.status !== 'sold').concat(s.positions.filter(p => p.status === 'sold').slice(-10));
    $('positions').innerHTML = pos.length ? pos.map(p => `<tr><td><b>${esc(p.name)}</b> <small>${esc(p.code)}</small>${p.dry ? ' <small>(흉내)</small>' : ''}</td><td><span class="grade ${esc(p.grade || 'B')}">${esc(p.grade || '-')}</span></td><td class="num">${fmt(p.quantity)}</td><td class="num">${fmt(Math.round(p.average))}</td><td>${esc(p.bought_on)}</td><td><span class="pill ${esc(p.status)}">${esc(names[p.status] || p.status)}</span></td><td>${esc(p.note || '')}</td></tr>`).join('')
      : '<tr><td colspan="7" class="empty">보유 없음</td></tr>';
    $('orders').innerHTML = s.orders.length ? [...s.orders].reverse().map(o => `<tr><td>${esc(o.at.slice(5, 19).replace('T', ' '))}</td><td><b>${esc(o.name)}</b> <small>${esc(o.code)}</small>${o.dry ? ' <small>(흉내)</small>' : ''}</td><td class="num">${fmt(o.filled)} / ${fmt(o.quantity)}</td><td class="num">${fmt(o.limit_price)}</td><td><span class="pill ${esc(o.status)}">${esc(names[o.status] || o.status)}</span>${o.order_no ? `<br><small>주문 ${esc(o.order_no)}</small>` : ''}</td><td>${esc(o.note)}</td></tr>`).join('')
      : '<tr><td colspan="6" class="empty">주문 기록 없음</td></tr>';
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
      link_exit: $('linkExit').checked, slot_amount: Number($('slotAmount').value), max_positions: Number($('maxPositions').value),
      total_cap: Number($('totalCap').value), min_base_eok: Number($('minBase').value), confirm_live: $('confirmLive').checked}));
    $('saveNote').textContent = '저장됨 ' + new Date().toLocaleTimeString('ko-KR');
  }); });
  $('stopAll').addEventListener('click', () => { if (confirm('비상 정지: 새 매수 주문을 막습니다. 이미 산 종목은 자동매도가 계속 들고 갑니다.')) act(async () => render(await api('/stop', {}))); });
  $('preview').addEventListener('click', () => act(async () => {
    $('pickMeta').textContent = '관제탑 자리를 보는 중…';
    preview = await api('/preview', {});
    renderPicks(preview.picks, preview.skipped, `미리보기 ${preview.at.slice(5, 16).replace('T', ' ')} · 11시 미리보기 포함 ${preview.readyCount}자리 중 통과 ${preview.picks.length}`);
    setTimeout(() => { preview = null; }, 600000);
  }));
  $('reconcile').addEventListener('click', () => act(async () => render(await api('/reconcile', {}))));
  refresh(); setInterval(refresh, 5000);
})();
