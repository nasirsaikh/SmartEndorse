(() => {
  const themeUrl = (theme) => theme === 'default'
    ? 'https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css'
    : `https://cdn.jsdelivr.net/npm/bootswatch@5.3.8/dist/${theme}/bootstrap.min.css`;

  const getCookie = (name) => document.cookie.split(';').map(v => v.trim()).find(v => v.startsWith(name + '='))?.split('=').slice(1).join('=') || '';

  window.fileDropzone = () => ({
    files: [],
    dragging: false,
    addFiles(list) {
      const merged = [...this.files];
      [...list].forEach(file => {
        const exists = merged.some(x => x.name === file.name && x.size === file.size && x.lastModified === file.lastModified);
        if (!exists) merged.push(file);
      });
      this.files = merged;
      this.sync();
    },
    removeFile(index) {
      this.files.splice(index, 1);
      this.files = [...this.files];
      this.sync();
    },
    sync() {
      const input = this.$refs.fileInput || this.$root.querySelector('input[type=file]');
      if (!input) return;
      const dt = new DataTransfer();
      this.files.forEach(file => dt.items.add(file));
      input.files = dt.files;
    },
    humanSize(size) {
      if (size < 1024) return `${size} B`;
      if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
      return `${(size / (1024 * 1024)).toFixed(1)} MB`;
    }
  });

  function initSelects(root = document) {
    root.querySelectorAll('select.searchable-select').forEach(select => {
      if (select.tomselect) {
        try { select.tomselect.sync(); } catch (_) {}
        return;
      }
      new TomSelect(select, {
        create: false,
        allowEmptyOption: true,
        maxOptions: 500,
        plugins: ['dropdown_input'],
        placeholder: select.dataset.placeholder || 'Search...'
      });
    });
  }

  function initToasts(root = document) {
    root.querySelectorAll('.toast').forEach(el => bootstrap.Toast.getOrCreateInstance(el).show());
  }

  function loadingButton(elt) {
    if (!elt) return null;
    if (elt.matches?.('button, input[type=submit]')) return elt;
    if (elt.tagName === 'FORM') return elt._submitter || elt.querySelector('button[type=submit],button:not([type]),input[type=submit]');
    return elt.closest?.('form')?._submitter || elt.closest?.('form')?.querySelector('button[type=submit],button:not([type]),input[type=submit]');
  }

  function startSpinner(button) {
    if (!button || button.dataset.loadingActive === '1') return;
    button.dataset.loadingActive = '1';
    button.dataset.originalHtml = button.innerHTML;
    button.disabled = true;
    const label = button.querySelector('.btn-label');
    const spinner = button.querySelector('.btn-spinner');
    if (label && spinner) {
      label.classList.add('opacity-50');
      spinner.classList.remove('d-none');
    } else {
      button.innerHTML = '<span class="spinner-border spinner-border-sm me-2" aria-hidden="true"></span>Processing…';
    }
  }

  function stopSpinner(button) {
    if (!button || button.dataset.loadingActive !== '1') return;
    if (button.dataset.originalHtml) button.innerHTML = button.dataset.originalHtml;
    button.disabled = false;
    delete button.dataset.loadingActive;
    delete button.dataset.originalHtml;
  }

  document.addEventListener('submit', e => { e.target._submitter = e.submitter; }, true);
  document.addEventListener('htmx:configRequest', e => {
    const token = getCookie('csrftoken');
    if (token) e.detail.headers['X-CSRFToken'] = decodeURIComponent(token);
  });
  document.addEventListener('htmx:beforeRequest', e => startSpinner(loadingButton(e.detail.elt)));
  document.addEventListener('htmx:afterRequest', e => stopSpinner(loadingButton(e.detail.elt)));
  document.addEventListener('htmx:responseError', e => {
    stopSpinner(loadingButton(e.detail.elt));
    const status = e.detail.xhr?.status;
    const suffix = status ? ` (HTTP ${status})` : '';
    showClientToast(
      'Request could not be completed',
      `SmartEndorse encountered an error${suffix}. The action was not completed. Review the message on the page or retry the request.`
    );
  });
  document.addEventListener('htmx:afterSwap', e => {
    initSelects(e.detail.target || document);
    initToasts(e.detail.target || document);
    notifyNewPortalItems(e.detail.target || document);
  });

  document.addEventListener('DOMContentLoaded', () => {
    initSelects();
    initToasts();
    notifyNewPortalItems(document);
  });

  document.body?.addEventListener('htmx:afterRequest', e => {
    if (e.detail.elt?.id === 'appearance-form' && e.detail.successful) {
      const theme = document.getElementById('theme-select')?.value || 'default';
      const modeChoice = document.getElementById('color-mode-select')?.value || 'auto';
      const mode = modeChoice === 'auto' ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : modeChoice;
      document.documentElement.setAttribute('data-bs-theme', mode);
      const link = document.getElementById('theme-css');
      if (link) link.href = themeUrl(theme);
      showClientToast('Appearance updated', 'Your theme and light/dark preference have been saved.');
    }
  });

  window.enableBrowserNotifications = async () => {
    if (!('Notification' in window)) return;
    if (Notification.permission === 'default') {
      try { await Notification.requestPermission(); } catch (_) {}
    }
  };

  function notifyNewPortalItems(root) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    root.querySelectorAll?.('[data-notification-id]').forEach(el => {
      const id = el.dataset.notificationId;
      const key = `smartendorse-notified-${id}`;
      if (localStorage.getItem(key)) return;
      localStorage.setItem(key, '1');
      const notification = new Notification(el.dataset.notificationTitle || 'SmartEndorse', {
        body: el.dataset.notificationMessage || '',
        icon: '/static/favicon.ico'
      });
      notification.onclick = () => { window.focus(); if (el.href) window.location.href = el.href; };
    });
  }

  function showClientToast(title, message) {
    const host = document.getElementById('toast-container');
    if (!host) return;
    const el = document.createElement('div');
    el.className = 'toast shadow border-0';
    el.innerHTML = `<div class="toast-header"><i class="bi bi-check-circle-fill text-success me-2"></i><strong class="me-auto"></strong><button class="btn-close" data-bs-dismiss="toast"></button></div><div class="toast-body"></div>`;
    el.querySelector('strong').textContent = title;
    el.querySelector('.toast-body').textContent = message;
    host.appendChild(el);
    const instance = new bootstrap.Toast(el, {delay: 4500});
    el.addEventListener('hidden.bs.toast', () => el.remove());
    instance.show();
  }
})();
