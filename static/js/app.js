// Nani's scripts (structure taken from the chef app). Each part only runs on pages that have
// the elements it needs; the script is loaded with `defer`, so the page is already parsed.
(() => {
  // --- Swipe left for the next week, right for the previous one (same links as the arrows). ---
  if (document.getElementById('week-next')) {
    const main = document.querySelector('main');
    let start = null;
    document.addEventListener('touchstart', (e) => {
      const t = e.touches[0];
      const skip = e.touches.length > 1 || e.target.closest('input, textarea, select, [contenteditable]');
      start = skip ? null : { x: t.clientX, y: t.clientY, time: Date.now() };
    }, { passive: true });
    document.addEventListener('touchend', (e) => {
      if (!start) return;
      const t = e.changedTouches[0];
      const dx = t.clientX - start.x;
      const dy = t.clientY - start.y;
      const quick = Date.now() - start.time < 700;
      start = null;
      // Clearly sideways and long enough, so scrolling up and down never changes the week.
      if (!quick || Math.abs(dx) < 70 || Math.abs(dx) < 2 * Math.abs(dy)) return;
      const link = document.getElementById(dx < 0 ? 'week-next' : 'week-prev');
      if (!link) return;
      main.classList.add(dx < 0 ? 'swipe-next' : 'swipe-prev');
      location.href = link.href;
    }, { passive: true });
    // Coming back with the browser's back button can show the page from cache, mid-animation.
    window.addEventListener('pageshow', () => main.classList.remove('swipe-next', 'swipe-prev'));
  }

  // --- Actions that need a second thought, e.g. deleting a trip: data-confirm="Question?" on
  //     the form, or on the button when only one of a form's buttons needs it. ---
  document.addEventListener('submit', (event) => {
    const question = event.submitter?.dataset.confirm || event.target.dataset.confirm;
    if (question && !window.confirm(question)) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);

  // --- Trips: earlier trips below the purpose field while typing (filtered like chef's recipe
  //     search). Picking one fills in purpose and km; a km typed by hand is never overwritten. ---
  document.querySelectorAll('input[data-trip-purpose]').forEach((input) => {
    const form = input.form;
    const list = form?.querySelector('.trip-suggest');
    const km = form?.querySelector('input[name=km]');
    if (!list || !km) return;
    const items = [...list.querySelectorAll('.suggestion')];
    const MAX_SHOWN = 6;

    const fillKm = (value) => {
      if (!km.value || km.value === km.dataset.suggested) {
        km.value = value;
        km.dataset.suggested = value;
      }
    };
    const filter = () => {
      const text = input.value.trim().toLowerCase();
      const words = text.split(/\s+/).filter(Boolean);
      let shown = 0;
      items.forEach((item) => {
        const match = words.length > 0 && item.dataset.search !== text && shown < MAX_SHOWN
          && words.every((w) => item.dataset.search.includes(w));
        item.hidden = !match;
        if (match) shown++;
      });
      list.hidden = shown === 0;
      // Typed an earlier purpose in full: take its km too.
      const exact = items.find((item) => item.dataset.search === text);
      if (exact) fillKm(exact.dataset.km);
    };

    input.addEventListener('input', filter);
    input.addEventListener('focus', filter);
    input.addEventListener('keydown', (e) => { if (e.key === 'Escape') list.hidden = true; });
    // Hide when focus leaves the field and its suggestions (a tap on a suggestion still counts).
    form.addEventListener('focusout', () => setTimeout(() => {
      if (!form.contains(document.activeElement) || document.activeElement === km) list.hidden = true;
    }, 150));
    items.forEach((item) => item.addEventListener('click', () => {
      input.value = item.dataset.purpose;
      fillKm(item.dataset.km);
      list.hidden = true;
      km.focus();
    }));
  });

  // --- Installable app: pass-through service worker. ---
  const swUrl = document.body.dataset.swUrl;
  if (swUrl && 'serviceWorker' in navigator) navigator.serviceWorker.register(swUrl);
})();
