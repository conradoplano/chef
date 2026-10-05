// Chef's scripts. Each part only runs on pages that have the elements it needs.
(() => {
  const fetchHeaders = { 'X-Requested-With': 'fetch' };

  // --- One-tap feedback on meal cards: saved in the background, no page reload. ---
  document.addEventListener('submit', async (event) => {
    const form = event.target.closest('.quick-feedback');
    if (!form) return;
    event.preventDefault();
    const button = event.submitter;
    const data = new FormData(form);
    data.set('rating', button.value);
    try {
      const response = await fetch(form.action, { method: 'POST', body: data, headers: fetchHeaders });
      if (!response.ok) throw new Error(response.status);
      const { rating } = await response.json();
      form.querySelectorAll('.qf').forEach((b) => b.setAttribute('aria-pressed', b.value === rating));
      form.parentElement.querySelector('.split')?.remove();
    } catch (e) {
      // Fall back to a normal form post (which reloads the page).
      form.append(Object.assign(document.createElement('input'), { type: 'hidden', name: 'rating', value: button.value }));
      form.submit();
    }
  });

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

  // --- Shopping list: tick items off and stay in sync with the other phones. ---
  const shopping = document.getElementById('shopping');
  if (shopping) {
    const stateUrl = shopping.dataset.stateUrl;
    let pending = 0;

    const render = (state) => {
      let bought = 0;
      const items = shopping.querySelectorAll('.shop-item');
      items.forEach((li) => {
        const by = state.checked[li.dataset.key];
        const checked = by !== undefined;
        if (checked) bought++;
        li.classList.toggle('checked', checked);
        li.querySelector('.item-main').setAttribute('aria-pressed', checked);
        li.querySelector('input[name=checked]').value = checked ? '0' : '1';
        li.querySelector('.by').textContent = checked && by ? '✓ ' + by : '';
      });
      const count = document.getElementById('bought-count');
      if (count) count.textContent = bought;
      const bar = document.getElementById('progress-bar');
      if (bar && items.length) bar.style.width = (100 * bought / items.length) + '%';
      // The menu changed on another device: items were added or removed.
      // Not while someone is typing a new item into a section's form.
      if (state.signature !== shopping.dataset.signature && !pending && !shopping.querySelector('.add-here-form:not([hidden])')) location.reload();
    };

    shopping.addEventListener('submit', async (event) => {
      const form = event.target.closest('.toggle-form');
      if (!form) return;
      event.preventDefault();
      const li = form.closest('.shop-item');
      const wanted = form.querySelector('input[name=checked]').value === '1';
      li.classList.toggle('checked', wanted);  // feels instant; the server's answer confirms it
      pending++;
      try {
        const response = await fetch(form.action, { method: 'POST', body: new FormData(form), headers: fetchHeaders });
        pending--;
        if (response.ok) render(await response.json());
      } catch (e) {
        pending--;
        li.classList.toggle('checked', !wanted);
      }
    });

    // A section's "+ Add item": opens a small form right there, for an item in that section.
    shopping.addEventListener('click', (event) => {
      const link = event.target.closest('.add-here-link, .add-here-cancel');
      if (!link) return;
      event.preventDefault();
      const form = document.getElementById(link.dataset.add);
      form.hidden = link.classList.contains('add-here-cancel') || !form.hidden;
      if (!form.hidden) form.querySelector('input[name="extra-name"]').focus();
    });

    const poll = async () => {
      if (document.visibilityState !== 'visible' || pending) return;
      try {
        const response = await fetch(stateUrl, { headers: fetchHeaders });
        if (response.ok) render(await response.json());
      } catch (e) { /* offline for a moment; try again next time */ }
    };
    setInterval(poll, 5000);
    document.addEventListener('visibilitychange', poll);

    // View switches, remembered per device.
    const viewToggle = (id, cls, labels) => {
      const button = document.getElementById(id);
      if (!button) return;
      const apply = (on) => {
        shopping.classList.toggle(cls, on);
        button.setAttribute('aria-pressed', on);
        button.textContent = labels[on ? 1 : 0];
      };
      try { apply(localStorage.getItem('chef-' + cls) === '1'); } catch (e) {}
      button.addEventListener('click', () => {
        const on = !shopping.classList.contains(cls);
        apply(on);
        try { localStorage.setItem('chef-' + cls, on ? '1' : '0'); } catch (e) {}
      });
    };
    const setupToggles = () => {
      viewToggle('hide-bought', 'hide-bought', ['Hide bought', 'Show bought']);
      viewToggle('hide-uses', 'hide-uses', ['Hide meals', 'Show meals']);
      // The item buttons: keep as a staple (pantry, freezer), remove, remove this week.
      viewToggle('hide-buttons', 'hide-buttons', ['Hide buttons', 'Show buttons']);
    };
    setupToggles();

    // Adding and removing items without reloading: the form is sent in the background, and the list (and
    // any message, e.g. the Undo bar) is replaced by the new one from the page the server answers with.
    // If that fails, the form is simply sent the normal way.
    const swap = (html) => {
      const page = new DOMParser().parseFromString(html, 'text/html');
      const fresh = page.getElementById('shopping');
      if (!fresh) return false;
      shopping.innerHTML = fresh.innerHTML;
      shopping.dataset.signature = fresh.dataset.signature;
      const main = document.querySelector('main');
      main.querySelectorAll(':scope > .flash').forEach((flash) => flash.remove());
      [...page.querySelectorAll('main > .flash')].reverse().forEach((flash) => main.prepend(flash));
      setupToggles();
      return true;
    };

    document.addEventListener('submit', async (event) => {
      const form = event.target;
      const bottomForm = form.closest('.add-extra');
      if (event.defaultPrevented || form.classList.contains('toggle-form') || !(shopping.contains(form) || bottomForm)) return;
      event.preventDefault();
      const reopen = form.classList.contains('add-here-form') ? form.id : '';
      pending++;  // no reload from the sync while this is on its way
      try {
        const response = await fetch(form.action, { method: 'POST', body: new FormData(form) });
        if (!response.ok || !swap(await response.text())) throw new Error('not swapped');
      } catch (e) {
        form.submit();
        return;
      } finally {
        pending--;
      }
      if (bottomForm) form.reset();
      if (reopen) {
        // Ready for the next item in the same section.
        const again = document.getElementById(reopen);
        if (again) { again.hidden = false; again.querySelector('input[name="extra-name"]').focus(); }
      }
    });
  }

  // --- Ingredients: add another empty row. ---
  const addRow = document.getElementById('add-row');
  if (addRow) {
    addRow.addEventListener('click', () => {
      const total = document.getElementById('id_ingredients-TOTAL_FORMS');
      const html = document.getElementById('empty-row').innerHTML.replace(/__prefix__/g, total.value);
      document.getElementById('ingredient-rows').insertAdjacentHTML('beforeend', html);
      total.value = Number(total.value) + 1;
      document.querySelector('#ingredient-rows .ingredient-row:last-child input').focus();
    });
  }

  // --- Menu being created: wait for it, then open the week. ---
  const planning = document.querySelector('#planning[data-status-url]');
  if (planning) {
    const poll = async () => {
      try {
        const response = await fetch(planning.dataset.statusUrl, { headers: fetchHeaders });
        const state = await response.json();
        if (state.status === 'done') { location.href = state.url; return; }
        if (state.finished) { location.reload(); return; }
      } catch (e) { /* try again */ }
      setTimeout(poll, 3000);
    };
    setTimeout(poll, 3000);
  }

  // --- Photos: scaled down in the browser before uploading (also turns iPhone HEIC into JPEG). ---
  const shrink = (file) => new Promise((resolve) => {
    if (!file.type.startsWith('image/')) return resolve(file);
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      const scale = Math.min(1, 2000 / Math.max(img.naturalWidth, img.naturalHeight));
      const canvas = document.createElement('canvas');
      canvas.width = Math.round(img.naturalWidth * scale);
      canvas.height = Math.round(img.naturalHeight * scale);
      canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
      URL.revokeObjectURL(url);
      canvas.toBlob((blob) => {
        if (!blob) return resolve(file);
        // Keep a JPEG that's already small; anything else (big, PNG, HEIC...) becomes the scaled JPEG.
        const keep = blob.size >= file.size && /jpe?g/.test(file.type);
        resolve(keep ? file : new File([blob], file.name.replace(/\.[^.]+$/, '') + '.jpg', { type: 'image/jpeg' }));
      }, 'image/jpeg', 0.85);
    };
    img.onerror = () => { URL.revokeObjectURL(url); resolve(file); };
    img.src = url;
  });
  document.querySelectorAll('input[type=file][data-resize]').forEach((input) => {
    input.addEventListener('change', async () => {
      const button = input.form.querySelector('[type=submit]');
      if (button) button.disabled = true;
      try {
        const files = await Promise.all([...input.files].map(shrink));
        const transfer = new DataTransfer();
        files.forEach((f) => transfer.items.add(f));
        input.files = transfer.files;
      } catch (e) { /* keep the originals; the server scales them down */ }
      if (button) button.disabled = false;
    });
  });

  // --- Choosing a recipe for a day: filter as you type. ---
  const pickSearch = document.getElementById('pick-search');
  if (pickSearch) {
    const filter = () => {
      const words = pickSearch.value.toLowerCase().split(/\s+/).filter(Boolean);
      let shown = 0;
      document.querySelectorAll('.pick-group').forEach((group) => {
        let inGroup = 0;
        group.querySelectorAll('.pick-card').forEach((card) => {
          const match = words.every((w) => card.dataset.search.includes(w));
          card.hidden = !match;
          if (match) inGroup++;
        });
        group.hidden = !inGroup;
        shown += inGroup;
      });
      document.getElementById('pick-empty').hidden = shown > 0;
    };
    pickSearch.addEventListener('input', filter);
    // Enter filters instead of submitting the form.
    pickSearch.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); filter(); } });
  }

  // --- Forms that need a second thought, e.g. removing a person: <form data-confirm="Question?">. ---
  document.addEventListener('submit', (event) => {
    const question = event.target.dataset.confirm;
    if (question && !window.confirm(question)) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);

  // --- Installable app: pass-through service worker. ---
  const swUrl = document.body.dataset.swUrl;
  if (swUrl && 'serviceWorker' in navigator) navigator.serviceWorker.register(swUrl);
})();
