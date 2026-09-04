// Dark-mode toggle, persisted in localStorage.
//
// Loaded from <head> without `defer` so the stored preference is applied
// before first paint — a deferred script would flash the light theme first.
// Extracted from an inline <script> so the CSP can stay at script-src 'self'.
(function () {
  try {
    const t = localStorage.getItem('theme');
    if (t === 'dark' || t === 'light') {
      document.documentElement.setAttribute('data-theme', t);
    }
  } catch (_) { /* localStorage may be disabled */ }

  document.addEventListener('DOMContentLoaded', function () {
    const btn = document.getElementById('theme-toggle');
    if (!btn) return;
    btn.addEventListener('click', () => {
      const current = document.documentElement.getAttribute('data-theme')
        || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
      const next = current === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      try { localStorage.setItem('theme', next); } catch (_) { }
    });
  });
})();
