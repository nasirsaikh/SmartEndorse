(() => {
  if (window.__smartEndorseAppReady) {
    window.initSmartEndorseUI?.(document);
    return;
  }
  window.__smartEndorseAppReady = true;
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
        <div class="glis-min-width-0">
          <div class="se-file-row-name"></div>
          <div class="se-file-row-meta"></div>
        </div>
        <button type="button" class="btn btn-danger btn-light glis-btn-xs" aria-label="Remove file">
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
    button.classList.add("disabled");
    button.querySelector(".btn-label")?.classList.add("opacity-75");
    button.querySelector(".btn-spinner")?.classList.remove("d-none");
    if (!button.querySelector(".btn-spinner")) {
      button.dataset.originalHtml = button.innerHTML;
      button.innerHTML = '<span class="spinner-border spinner-border-sm"></span><span>Processing…</span>';
    }
  }

  function stopSpinner(button) {
    if (!button || button.dataset.loadingActive !== "1") return;
    if (button.dataset.originalHtml) button.innerHTML = button.dataset.originalHtml;
    button.disabled = false;
    button.classList.remove("disabled");
    button.querySelector(".btn-label")?.classList.remove("opacity-75");
    button.querySelector(".btn-spinner")?.classList.add("d-none");
    delete button.dataset.loadingActive;
    delete button.dataset.originalHtml;
  }

  function showToast(title, message, level = "success") {
    const host = document.getElementById("toast-container");
    if (!host) return;
    const item = document.createElement("div");
    const alertClass = level === "error" ? "alert-danger" : level === "warning" ? "alert-warning" : level === "info" ? "alert-info" : "alert-success";
    item.className = `alert ${alertClass}`;
    const wrapper = document.createElement("div");
    const strong = document.createElement("strong");
    const text = document.createElement("span");
    strong.textContent = title;
    text.textContent = message;
    wrapper.className = "d-grid gap-1";
    text.className = "glis-text-xs";
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
    document.documentElement.setAttribute("data-bs-theme", theme);
    document.documentElement.dataset.themePreference = choice;
    localStorage.setItem("smartendorse-color-mode", choice);
    updateThemeIcon();
    syncChartTheme();
  }

  function updateThemeIcon() {
    const icon = document.getElementById("theme-toggle-icon");
    if (!icon) return;
    const dark = document.documentElement.getAttribute("data-theme") === "dark";
    icon.className = dark ? "bi bi-sun fs-5" : "bi bi-moon-stars fs-5";
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

  function chartTheme() {
    return document.documentElement.getAttribute("data-bs-theme") === "dark" ? "dark" : "light";
  }

  function initCharts(root = document) {
    if (!window.ApexCharts) return;
    root.querySelectorAll?.("[data-chart-config]").forEach(host => {
      if (host._chart) return;
      const source = document.getElementById(host.dataset.chartConfig);
      if (!source) return;
      const data = JSON.parse(source.textContent);
      const maximum = Math.max(1, ...data.series.flatMap(series => series.data));
      const chart = new ApexCharts(host, {
        chart: {type: data.type, height: 280, toolbar: {show: false}, background: "transparent", fontFamily: "inherit"},
        theme: {mode: chartTheme()}, colors: ["#2563eb"], series: data.series,
        xaxis: {categories: data.categories}, stroke: {width: data.type === "line" ? 2 : 0},
        markers: {size: data.type === "line" ? 4 : 0},
        yaxis: {min: 0, max: maximum, tickAmount: Math.min(maximum, 5), labels: {formatter: value => Math.round(value).toLocaleString()}},
        dataLabels: {enabled: false}, noData: {text: "No endorsements yet"},
        plotOptions: {bar: {borderRadius: 4, columnWidth: "50%"}}, grid: {borderColor: chartTheme() === "dark" ? "#333" : "#e2e4e9"},
      });
      host._chart = chart;
      chart.render();
    });
  }

  function syncChartTheme() {
    document.querySelectorAll("[data-chart-config]").forEach(host => host._chart?.updateOptions({theme: {mode: chartTheme()}, grid: {borderColor: chartTheme() === "dark" ? "#333" : "#e2e4e9"}}));
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

  function syncNotificationBadge() {
    const panel = document.getElementById("notification-panel");
    const badge = document.getElementById("notification-badge");
    if (!panel) return;
    const unreadText = panel.querySelector("[data-unread-count]")?.dataset.unreadCount;
    if (unreadText == null) return;
    const unread = Number(unreadText) || 0;
    if (!unread) {
      badge?.remove();
      return;
    }
    if (badge) {
      badge.textContent = String(unread);
      return;
    }
    const button = panel.closest(".dropdown")?.querySelector("button");
    if (!button) return;
    const next = document.createElement("span");
    next.id = "notification-badge";
    next.className = "badge text-bg-danger glis-badge-small position-absolute glis-end-1 glis-top-1";
    next.textContent = String(unread);
    button.appendChild(next);
  }

  function initialize(root = document) {
    initDropzones(root);
    initPlanSumAssured(root);
    notifyNewPortalItems(root);
    updateThemeIcon();
    syncNotificationBadge();
    initCharts(root);
  }

  window.initSmartEndorseUI = initialize;

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
  document.addEventListener("htmx:beforeCleanupElement", event => {
    event.detail.elt.querySelectorAll?.("[data-chart-config]").forEach(host => host._chart?.destroy());
    const modal = event.detail.elt.matches?.(".modal") ? event.detail.elt : null;
    if (modal) bootstrap.Modal.getInstance(modal)?.dispose();
  });

  document.addEventListener("click", event => {
    if (event.target.closest("#sidebar-toggle")) {
      const mini = document.body.classList.toggle("se-sidebar-mini");
      const button = document.getElementById("sidebar-toggle");
      button.setAttribute("aria-expanded", String(!mini));
      button.setAttribute("aria-label", mini ? "Expand navigation" : "Collapse navigation");
    }
    if (event.target.closest("#theme-toggle")) {
      event.preventDefault();
      toggleTheme();
    }
  });

  document.addEventListener("DOMContentLoaded", () => {
    const saved = localStorage.getItem("smartendorse-color-mode");
    if (saved && ["light", "dark", "auto"].includes(saved)) applyTheme(saved);
    initialize(document);
    document.querySelectorAll("#portal-sidebar a").forEach(link => {
      link.addEventListener("click", () => bootstrap.Offcanvas.getInstance(document.getElementById("portal-navigation"))?.hide());
    });
  });

  matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => {
    if ((document.documentElement.dataset.themePreference || "auto") === "auto") applyTheme("auto");
  });
})();
