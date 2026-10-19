/* 차단기 표시 (네 매매 화면 공용). 상태는 각 화면이 1초마다 받는 snapshot.breakers 를 쓴다.
   걸리면 새 매수만 멈춘다 — 매도와 미체결 취소는 그대로 나간다. 규칙은 app/breakers.py */
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const fmt = (n) => (n === null || n === undefined ? '—' : Number(n).toLocaleString('ko-KR'));

  function say(text, bad) {
    const box = $('feedback');
    if (!box) { if (bad) alert(text); return; }
    box.textContent = text;
    box.hidden = false;
    setTimeout(() => { box.hidden = true; }, 8000);
  }

  async function post(path) {
    const got = await fetch('/api/breakers', { headers: { Accept: 'application/json' } });
    if (!got.ok) throw new Error('차단기 상태를 못 읽었습니다');
    const state = await got.json();
    const res = await fetch('/api/breakers' + path, { method: 'POST', headers: { 'X-Breakers-Token': state.token } });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || `실패 (${res.status})`);
    return body;
  }

  window.renderBreakers = (b) => {
    const el = $('breakerBar');
    if (!el) return;
    if (!b || !b.on) { el.hidden = true; return; }
    el.hidden = false;
    const lim = b.limits || {};
    const money = b.equity
      ? `예탁자산 ${fmt(b.equity)}원` + (b.drop === null || b.drop === undefined ? '' : ` (아침 ${fmt(b.baseEquity)}원 대비 ${b.drop > 0 ? '+' : ''}${b.drop}%)`)
      : '예탁자산 확인 전';
    const counts = `오늘 매수 ${b.buys}건 · 매도 ${b.sells}건 · 주문 거부 ${b.rejects}/${lim.rejectMax}건`;
    if (b.why) {
      el.className = 'breakers tripped';
      el.innerHTML = `<div><strong>🛑 차단기: ${esc(b.why)}</strong>`
        + `<p><b>새 매수만 멈췄습니다.</b> 보유 종목의 매도와 미체결 취소는 그대로 나갑니다.`
        + ` · ${esc(money)} · ${esc(counts)}</p></div>`
        + '<button type="button" id="breakerRelease">차단기 해제</button>';
      $('breakerRelease').addEventListener('click', async (e) => {
        e.target.disabled = true;
        try {
          window.renderBreakers(await post('/release'));
          say('차단기를 해제했습니다. 오늘은 그 차단기가 다시 걸리지 않습니다.');
        } catch (err) {
          e.target.disabled = false;
          say('차단기 해제 실패: ' + err.message, true);
        }
      });
      return;
    }
    const notes = [];
    if (b.baseLate) notes.push('아침 기준을 못 잡았습니다 (서버가 장중에 떴습니다)');
    if ((b.released || []).length) notes.push(`오늘 해제: ${b.released.join(', ')}`);
    if (b.error) notes.push(b.error);
    el.className = 'breakers';
    el.innerHTML = `<div><strong>🛡 차단기 정상</strong><p>${esc(money)} · ${esc(counts)}`
      + ` · 한도: 예탁자산 ${lim.equityDrop}% · 거부 ${lim.rejectMax}건 · 매수 하루 ${lim.buyMaxDay}건/한 종목 ${lim.buyMaxCode}건`
      + (notes.length ? ` · ${esc(notes.join(' · '))}` : '') + '</p></div>';
  };
})();
