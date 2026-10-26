(() => {
  const root = document.querySelector('[data-support-version]');
  if (!root) return;
  let dirty = false, running = false;
  document.querySelectorAll('form').forEach(form => form.addEventListener('input', () => { dirty = true; }));
  const notice = document.getElementById('support-live-notice');
  async function poll() {
    if (document.hidden || running) return;
    running = true;
    try {
      const response = await fetch(root.dataset.supportPulse || '/support/pulse/', {credentials: 'same-origin', cache: 'no-store', headers: {'Accept': 'application/json'}});
      if (!response.ok || response.redirected) throw new Error('unavailable');
      const data = await response.json();
      if (data.version !== root.dataset.supportVersion) {
        if (!dirty) { location.reload(); return; }
        notice.hidden = false;
        notice.textContent = 'Há atualizações neste chamado. Atualize a página antes de salvar; seu texto não será apagado automaticamente.';
      }
    } catch (_) {
      notice.hidden = false;
      notice.textContent = 'Atualização automática indisponível. Verifique a conexão e atualize a página.';
    } finally { running = false; }
  }
  setInterval(poll, 5000);
})();
