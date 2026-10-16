// Keeps the sidebar's "what the daemon is doing" line fresh from
// /activity.json. Progressive enhancement: the server already rendered the
// line, so without JS it is simply as of page load. A file rather than an
// inline <script> because the CSP is script-src 'self'.
(function () {
  const POLL_MS = 10000;

  function label(a) {
    const phase = String(a.phase || 'idle').replace(/_/g, ' ');
    const text = phase.charAt(0).toUpperCase() + phase.slice(1);
    return a.playlist_name ? `${text}: ${a.playlist_name}` : text;
  }

  document.addEventListener('DOMContentLoaded', function () {
    const el = document.getElementById('shell-activity');
    if (!el || !el.dataset.src) return;
    const text = el.querySelector('.text');

    async function refresh() {
      if (document.hidden) return;
      try {
        const r = await fetch(el.dataset.src, { headers: { Accept: 'application/json' } });
        if (!r.ok) return;
        const a = await r.json();
        el.dataset.phase = a.phase || 'idle';
        text.textContent = label(a);  // text, never HTML
        text.title = a.detail || '';
      } catch (_) { /* offline or restarting: keep the last value */ }
    }
    setInterval(refresh, POLL_MS);
  });
})();
