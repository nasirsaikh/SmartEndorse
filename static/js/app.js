(() => {
  const getCookie = (name) =>
    document.cookie.split(";").map(v => v.trim()).find(v => v.startsWith(name + "="))
      ?.split("=").slice(1).join("=") || "";

  window.fileDropzone = () => ({
    files: [],
    dragging: false,
    addFiles(list) {
      const merged = [...this.files];
      [...list].forEach(file => {
        const exists = merged.some(x =>
          x.name === file.name && x.size === file.size && x.lastModified === file.lastModified
        );
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
      const input = this.$refs.fileInput || this.$root.querySelector('input[type="file"]');
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
    root.querySelectorAll("select.searchable-select").forEach(select => {
      if (select.tomselect) {
        try { select.tomselect.sync(); } catch (_) {}
        return;
      }
      new TomSelect(select, {
        create: false,
        allowEmptyOption: true,
        maxOptions: 500,
        plugins: ["dropdown_input"],
        placeholder: select.dataset.placeholder || "Search…"
      });
    });
  }

  function loadingButton(elt) {
    if (!elt) return null;
    if (elt.matches?.('button, input[type="submit"]')) return elt;
    if (elt.tagName === "FORM") {
      return elt._submitter || elt.querySelector('button[type="submit"], input[type="submit"]');
    }
    const form = elt.closest?.("form");
    return form?._submitter || form?.querySelector('button[type="submit"], input[type="submit"]');
  }

  function startSpinner(button) {
    if (!button || button.dataset.loadingActive === "1") return;
    button.dataset.loadingActive = "1";
    button.disabled = true;
    button.classList.add("is-loading");

    if (!button.querySelector?.(".portal-spinner")) {
      button.dataset.originalHtml = button.innerHTML;
      button.innerHTML = '<span class="btn-label">Processing…</span><span class="portal-spinner" aria-hidden="true"></span>';
    }
  }

  function stopSpinner(button) {
    if (!button || button.dataset.loadingActive !== "1") return;
    if (button.dataset.originalHtml) button.innerHTML = button.dataset.originalHtml;
    button.disabled = false;
    button.classList.remove("is-loading");
    delete button.dataset.loadingActive;
    delete button.dataset.originalHtml;
  }

  function showClientToast(title, message, level = "success") {
    const host = document.getElementById("portal-toast-container");
    if (!host) return;
    const toast = document.createElement("div");
    toast.className = `portal-toast ${level}`;
    const heading = document.createElement("strong");
    const body = document.createElement("div");
    heading.textContent = title;
    body.textContent = message;
    toast.append(heading, body);
    host.appendChild(toast);
    window.setTimeout(() => toast.remove(), 5000);
  }

  function applyColorMode(choice) {
    const actual = choice === "auto"
      ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
      : choice;
    document.documentElement.dataset.theme = actual;
  }

  window.enableBrowserNotifications = async () => {
    if (!("Notification" in window)) return;
    if (Notification.permission === "default") {
      try { await Notification.requestPermission(); } catch (_) {}
    }
  };

  function notifyNewPortalItems(root) {
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    root.querySelectorAll?.("[data-notification-id]").forEach(el => {
      const id = el.dataset.notificationId;
      const key = `smartendorse-notified-${id}`;
      if (localStorage.getItem(key)) return;
      localStorage.setItem(key, "1");
      const notification = new Notification(el.dataset.notificationTitle || "SmartEndorse", {
        body: el.dataset.notificationMessage || ""
      });
      notification.onclick = () => {
        window.focus();
        if (el.href) window.location.href = el.href;
      };
    });
  }

  document.addEventListener("submit", e => {
    e.target._submitter = e.submitter;
  }, true);

  document.addEventListener("htmx:configRequest", e => {
    const token = getCookie("csrftoken");
    if (token) e.detail.headers["X-CSRFToken"] = decodeURIComponent(token);
  });

  document.addEventListener("htmx:beforeRequest", e => startSpinner(loadingButton(e.detail.elt)));
  document.addEventListener("htmx:afterRequest", e => stopSpinner(loadingButton(e.detail.elt)));

  document.addEventListener("htmx:responseError", e => {
    stopSpinner(loadingButton(e.detail.elt));
    const status = e.detail.xhr?.status;
    showClientToast(
      "Request could not be completed",
      `SmartEndorse encountered an error${status ? ` (HTTP ${status})` : ""}. The action was not completed.`,
      "error"
    );
  });

  document.addEventListener("htmx:afterSwap", e => {
    initSelects(e.detail.target || document);
    notifyNewPortalItems(e.detail.target || document);
  });

  document.addEventListener("DOMContentLoaded", () => {
    initSelects();
    notifyNewPortalItems(document);

    const navToggle = document.getElementById("portal-nav-toggle");
    navToggle?.addEventListener("click", () => document.body.classList.toggle("portal-nav-open"));

    document.querySelectorAll(".portal-sidebar a").forEach(link => {
      link.addEventListener("click", () => document.body.classList.remove("portal-nav-open"));
    });
  });

  document.body?.addEventListener("htmx:afterRequest", e => {
    if (e.detail.elt?.id === "appearance-form" && e.detail.successful) {
      const mode = document.getElementById("color-mode-select")?.value || "auto";
      applyColorMode(mode);
      showClientToast("Appearance updated", "Your Django admin-style color mode has been saved.");
    }
  });

  matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => {
    const select = document.getElementById("color-mode-select");
    if (select?.value === "auto") applyColorMode("auto");
  });
})();
