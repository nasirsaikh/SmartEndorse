(() => {
  const getCookie = (name) =>
    document.cookie.split(";").map(v => v.trim()).find(v => v.startsWith(name + "="))
      ?.split("=").slice(1).join("=") || "";

  const humanSize = (size) => {
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  };

  function mergeFiles(current, incoming) {
    const merged = [...current];
    [...incoming].forEach(file => {
      const duplicate = merged.some(existing =>
        existing.name === file.name &&
        existing.size === file.size &&
        existing.lastModified === file.lastModified
      );
      if (!duplicate) merged.push(file);
    });
    return merged;
  }

  function assignFiles(input, files) {
    const transfer = new DataTransfer();
    files.forEach(file => transfer.items.add(file));
    input.files = transfer.files;
  }

  function renderDropzone(zone, input, files) {
    const list = zone.querySelector(".se-file-list");
    const count = zone.querySelector("[data-file-count]");
    if (count) count.textContent = files.length ? `${files.length} file${files.length === 1 ? "" : "s"} selected` : "No files selected";
    if (!list) return;

    list.innerHTML = "";
    files.forEach((file, index) => {
      const row = document.createElement("div");
      row.className = "se-file-row";
      row.innerHTML = `
        <div class="min-w-0">
          <div class="se-file-row-name"></div>
          <div class="se-file-row-meta"></div>
        </div>
        <button type="button" class="btn btn-error btn-ghost btn-xs" aria-label="Remove file">
          <i class="bi bi-x-lg"></i>
        </button>`;
      row.querySelector(".se-file-row-name").textContent = file.name;
      row.querySelector(".se-file-row-meta").textContent = humanSize(file.size);
      row.querySelector("button").addEventListener("click", event => {
        event.stopPropagation();
        const next = files.filter((_, i) => i !== index);
        zone._files = next;
        assignFiles(input, next);
        renderDropzone(zone, input, next);
      });
      list.appendChild(row);
    });
  }

  function initDropzones(root = document) {
    root.querySelectorAll(".se-dropzone").forEach(zone => {
      if (zone.dataset.dropzoneReady === "1") return;
      const input = zone.querySelector('input[type="file"]') || zone.closest("form")?.querySelector('input[type="file"][data-drop-input]');
      if (!input) return;

      zone.dataset.dropzoneReady = "1";
      zone._files = [...input.files];
      renderDropzone(zone, input, zone._files);

      zone.addEventListener("click", event => {
        if (event.target.closest("button,a,input,label")) return;
        input.click();
      });
      zone.querySelectorAll("[data-browse-files]").forEach(trigger => {
        trigger.addEventListener("click", event => {
          event.preventDefault();
          event.stopPropagation();
          input.click();
        });
      });

      input.addEventListener("change", () => {
        zone._files = mergeFiles(zone._files || [], input.files);
        assignFiles(input, zone._files);
        renderDropzone(zone, input, zone._files);
      });

      ["dragenter", "dragover"].forEach(name => zone.addEventListener(name, event => {
        event.preventDefault();
        event.stopPropagation();
        zone.classList.add("is-dragging");
      }));
      ["dragleave", "drop"].forEach(name => zone.addEventListener(name, event => {
        event.preventDefault();
        event.stopPropagation();
        zone.classList.remove("is-dragging");
      }));
      zone.addEventListener("drop", event => {
        if (!event.dataTransfer?.files?.length) return;
        zone._files = mergeFiles(zone._files || [], event.dataTransfer.files);
        assignFiles(input, zone._files);
        renderDropzone(zone, input, zone._files);
      });
    });
  }

  async function updatePlanSumAssured(select) {
    if (!select?.dataset.planSumAssured) return;
    const target = document.querySelector('[data-plan-sum-target="true"]');
    if (!target) return;
    const planId = select.value;
    target.value = "";
    if (!planId) {
      target.placeholder = "Select a plan";
      return;
    }
    try {
      const url = new URL(select.dataset.planSumUrl || "/policy-plans/sum-assured/", window.location.origin);
      url.searchParams.set("plan", planId);
      const response = await fetch(url, {headers: {"X-Requested-With": "XMLHttpRequest"}});
      if (!response.ok) throw new Error("Plan lookup failed");
      const data = await response.json();
      target.value = data.sum_assured ?? "";
      target.placeholder = data.sum_assured ? "" : "Not configured for this plan";
    } catch (_) {
      target.value = "";
      target.placeholder = "Unable to load plan sum assured";
    }
  }

  function initPlanSumAssured(root = document) {
    const selects = [];
    if (root.matches?.('select[data-plan-sum-assured="true"]')) selects.push(root);
    root.querySelectorAll?.('select[data-plan-sum-assured="true"]').forEach(el => selects.push(el));
    selects.forEach(select => {
      if (select.dataset.planSumReady === "1") return;
      select.dataset.planSumReady = "1";
      select.addEventListener("change", () => updatePlanSumAssured(select));
      if (select.value) updatePlanSumAssured(select);
    });
  }

  function initSelects(root = document) {
    const selects = [];
    if (root.matches?.("select.searchable-select")) selects.push(root);
    root.querySelectorAll?.("select.searchable-select").forEach(select => selects.push(select));

    selects.forEach(select => {
      if (select.tomselect) {
        if (root === select) {
          try { select.tomselect.destroy(); } catch (_) {}
        } else {
          return;
        }
      }
      new TomSelect(select, {
        create: false,
        allowEmptyOption: true,
        maxOptions: 500,
        plugins: ["dropdown_input"],
        placeholder: select.dataset.placeholder || "Search…",
        closeAfterSelect: true,
        onChange() {
          if (select.dataset.planSumAssured === "true") updatePlanSumAssured(select);
        },
        onDropdownOpen() {
          this.positionDropdown();
        }
      });
    });
  }

  function loadingButton(elt) {
    if (!elt) return null;
    if (elt.matches?.('button, input[type="submit"]')) return elt;
    if (elt.tagName === "FORM") return elt._submitter || elt.querySelector('button[type="submit"],input[type="submit"]');
    const form = elt.closest?.("form");
    return form?._submitter || form?.querySelector('button[type="submit"],input[type="submit"]');
  }

  function startSpinner(button) {
    if (!button || button.dataset.loadingActive === "1") return;
    button.dataset.loadingActive = "1";
    button.disabled = true;
    button.classList.add("btn-disabled");
    button.querySelector(".btn-label")?.classList.add("opacity-60");
    button.querySelector(".btn-spinner")?.classList.remove("hidden");
    if (!button.querySelector(".btn-spinner")) {
      button.dataset.originalHtml = button.innerHTML;
      button.innerHTML = '<span class="loading loading-spinner loading-xs"></span><span>Processing…</span>';
    }
  }

  function stopSpinner(button) {
    if (!button || button.dataset.loadingActive !== "1") return;
    if (button.dataset.originalHtml) button.innerHTML = button.dataset.originalHtml;
    button.disabled = false;
    button.classList.remove("btn-disabled");
    button.querySelector(".btn-label")?.classList.remove("opacity-60");
    button.querySelector(".btn-spinner")?.classList.add("hidden");
    delete button.dataset.loadingActive;
    delete button.dataset.originalHtml;
  }

  function showToast(title, message, level = "success") {
    const host = document.getElementById("toast-container");
    if (!host) return;
    const item = document.createElement("div");
    const alertClass = level === "error" ? "alert-error" : level === "warning" ? "alert-warning" : level === "info" ? "alert-info" : "alert-success";
    item.className = `alert ${alertClass}`;
    const wrapper = document.createElement("div");
    const strong = document.createElement("strong");
    const text = document.createElement("span");
    strong.textContent = title;
    text.textContent = message;
    wrapper.className = "grid gap-0.5";
    text.className = "text-xs";
    wrapper.append(strong, text);
    item.appendChild(wrapper);
    host.appendChild(item);
    setTimeout(() => item.remove(), 5000);
  }

  function resolveTheme(choice) {
    return choice === "auto"
      ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
      : choice;
  }

  function applyTheme(choice) {
    const theme = resolveTheme(choice);
    document.documentElement.setAttribute("data-theme", theme);
    document.documentElement.dataset.themePreference = choice;
    updateThemeIcon();
    syncPlotlyTheme();
  }

  function updateThemeIcon() {
    const icon = document.getElementById("theme-toggle-icon");
    if (!icon) return;
    const dark = document.documentElement.getAttribute("data-theme") === "dark";
    icon.className = dark ? "bi bi-sun text-lg" : "bi bi-moon-stars text-lg";
  }

  async function toggleTheme() {
    const current = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
    const next = current === "dark" ? "light" : "dark";
    applyTheme(next);
    try {
      const body = new URLSearchParams({color_mode: next});
      const response = await fetch("/preferences/", {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
          "X-CSRFToken": decodeURIComponent(getCookie("csrftoken")),
          "X-Requested-With": "XMLHttpRequest"
        },
        body
      });
      if (!response.ok) throw new Error("Could not save theme");
    } catch (_) {
      showToast("Theme changed locally", "The visual mode changed, but the preference could not be saved.", "warning");
    }
  }

  function syncPlotlyTheme() {
    if (!window.Plotly) return;
    const dark = document.documentElement.getAttribute("data-theme") === "dark";
    const text = dark ? "#d7fbea" : "#17302a";
    const grid = dark ? "rgba(117,255,207,.10)" : "rgba(10,75,60,.10)";
    document.querySelectorAll(".plotly-graph-div").forEach(plot => {
      try {
        Plotly.relayout(plot, {
          "paper_bgcolor": "rgba(0,0,0,0)",
          "plot_bgcolor": "rgba(0,0,0,0)",
          "font.color": text,
          "xaxis.gridcolor": grid,
          "yaxis.gridcolor": grid,
          "xaxis.zerolinecolor": grid,
          "yaxis.zerolinecolor": grid
        });
      } catch (_) {}
    });
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

  function initialize(root = document) {
    initDropzones(root);
    initSelects(root);
    initPlanSumAssured(root);
    notifyNewPortalItems(root);
    updateThemeIcon();
    setTimeout(syncPlotlyTheme, 50);
  }

  document.addEventListener("submit", event => {
    event.target._submitter = event.submitter;
  }, true);

  document.addEventListener("htmx:configRequest", event => {
    const token = getCookie("csrftoken");
    if (token) event.detail.headers["X-CSRFToken"] = decodeURIComponent(token);
  });

  document.addEventListener("htmx:beforeRequest", event => startSpinner(loadingButton(event.detail.elt)));
  document.addEventListener("htmx:afterRequest", event => stopSpinner(loadingButton(event.detail.elt)));
  document.addEventListener("htmx:responseError", event => {
    stopSpinner(loadingButton(event.detail.elt));
    const status = event.detail.xhr?.status;
    showToast(
      "Request failed",
      `The action was not completed${status ? ` (HTTP ${status})` : ""}. Check the page message and try again.`,
      "error"
    );
  });
  document.addEventListener("htmx:afterSwap", event => initialize(event.detail.target || document));

  document.addEventListener("DOMContentLoaded", () => {
    initialize(document);
    document.getElementById("theme-toggle")?.addEventListener("click", toggleTheme);
    document.querySelectorAll(".drawer-side a").forEach(link => {
      link.addEventListener("click", () => {
        const drawer = document.getElementById("portal-drawer");
        if (drawer) drawer.checked = false;
      });
    });
  });

  matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => {
    if ((document.documentElement.dataset.themePreference || "auto") === "auto") applyTheme("auto");
  });
})();
