/* Navigation only. Never calls account, condition or order APIs. */
(() => {
  'use strict';
  const routes = {
    '/horizon.html': {name:'themeRadarHorizon', width:1600},
    '/auto-buy.html': {name:'themeRadarAutoBuy', width:1400},
    '/autoexit.html': {name:'themeRadarAutoExit', width:1280},
    '/custom-exit.html': {name:'themeRadarCustomExit', width:1680},
    '/closebet.html': {name:'themeRadarCloseBet', width:1500},
    '/horizonbuy.html': {name:'themeRadarHorizonBuy', width:1500},
    '/firebar.html': {name:'themeRadarFireBar', width:1500},
    '/leaderdip.html': {name:'themeRadarLeaderDip', width:1500},
  };
  const current = routes[location.pathname];
  if (current) window.name = current.name;
  const openedWindows = new Map();
  function popupBlocked(url) {
    let note = document.getElementById('windowOpenNotice');
    if (!note) {
      note = document.createElement('div');
      note.id = 'windowOpenNotice'; note.setAttribute('role', 'status');
      note.style.cssText = 'position:fixed;bottom:18px;right:18px;left:18px;z-index:1000;padding:12px 16px;background:#253247;color:#edf4ff;border:1px solid #7091bd;border-radius:8px;font:13px/1.7 system-ui;display:flex;align-items:center;gap:12px;flex-wrap:wrap;box-shadow:0 4px 24px #0006';
      document.body.append(note);
    }
    note.replaceChildren(document.createTextNode('새 창이 열리지 않으면 브라우저에서 팝업을 허용해 주세요.'));
    const fallback = document.createElement('a');
    fallback.href = url.href; fallback.target = '_blank'; fallback.rel = 'noopener';
    fallback.dataset.windowFallback = 'true'; fallback.textContent = '새 탭으로 열기 ↗';
    fallback.style.cssText = 'color:#b9dcff;text-decoration:underline;font-weight:700';
    const close = document.createElement('button'); close.type = 'button'; close.textContent = '닫기';
    close.addEventListener('click', () => note.remove()); note.append(fallback, close);
  }
  document.addEventListener('click', event => {
    if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest?.('a[href]');
    if (!link || link.hasAttribute('download') || link.dataset?.windowFallback) return;
    const url = new URL(link.href, location.href);
    if(url.origin === location.origin && url.pathname === '/autoexit.html'){url.pathname='/custom-exit.html';url.hash='horizon';link.href=url.href;}
    const route = routes[url.pathname];
    if (url.origin !== location.origin || !route) return;
    // Anchors within the current screen should continue to scroll normally.
    if (url.pathname === location.pathname) return;
    link.target = route.name;
    const width = Math.min(route.width, window.screen.availWidth || route.width);
    const height = Math.min(960, window.screen.availHeight || 960);
    let opened = openedWindows.get(route.name);
    try {
      const needsOpen = !opened || opened.closed;
      if (needsOpen) opened = window.open(url.href, route.name, `popup=yes,width=${width},height=${height},resizable=yes,scrollbars=yes`);
      if (!opened) { popupBlocked(url); return; } // Keep the native link as fallback.
      event.preventDefault();
      openedWindows.set(route.name, opened);
      let samePage = false;
      try {
        const existing = new URL(opened.location.href);
        samePage = existing.origin === url.origin && existing.pathname === url.pathname && existing.search === url.search;
      } catch (_) { /* A reused window may have navigated to another origin. */ }
      if (!needsOpen && !samePage) opened.location.href = url.href;
      else if (!needsOpen && url.hash && opened.location.hash !== url.hash) opened.location.hash = url.hash;
      opened.focus();
    } catch (_) {
      // Restore a normal link if the browser denied access to a popup window.
      if (event.defaultPrevented) window.open(url.href, route.name);
    }
  });
})();
