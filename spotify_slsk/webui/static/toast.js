// Toasts for htmx-driven actions. A route answering an htmx request sets
//   HX-Trigger: {"toast": {"items": [{"message": "...", "level": "success"}]}}
// (webui.helpers.notify_user); htmx fires a "toast" event that bubbles to
// <body>, and this renders each item for a few seconds. A file rather than
// an inline <script> because the CSP is script-src 'self'.
(function () {
  const TTL_MS = 6000;

  function show(item) {
    const box = document.getElementById('toasts');
    if (!box || !item || !item.message) return;
    const el = document.createElement('p');
    const level = item.level === 'error' ? 'error' : 'success';
    el.className = `flash flash-${level} toast`;
    el.textContent = item.message;  // text, never HTML
    box.appendChild(el);
    setTimeout(() => el.remove(), TTL_MS);
  }

  document.addEventListener('toast', (event) => {
    const items = (event.detail && event.detail.items) || [];
    items.forEach(show);
  });
})();
