/*
 * Page behaviour: hydrate [data-chart] elements with ECharts (also after HTMX
 * swaps), result switches (pair curves, merged episode table), analysis tabs (ARIA tabs with arrow keys), info
 * tooltips, confirm dialogs, the settings drawer (opened from the run bar, kept open with its unapplied edits across
 * HTMX swaps), the Upload drop zone and its Separate/Overlay view switch, progress for plain form posts and on the
 * button that started a computation, and the Export screen's chart capture for the PDF report and the ZIP.
 */
(function () {
  "use strict";

  // Styles that only make sense with this script (e.g. the drop zone's upload button is replaced by auto-submit).
  document.documentElement.classList.add("js");

  function labels() {
    var element = document.getElementById("gsr-chart-labels");
    try {
      return element ? JSON.parse(element.textContent) || {} : {};
    } catch (error) {
      return {};
    }
  }

  function readData(id) {
    var element = id && document.getElementById(id);
    return element ? JSON.parse(element.textContent) : null;
  }

  function isVisible(element) {
    return element.offsetParent !== null && element.clientWidth > 0;
  }

  function initChart(element) {
    if (element._gsrChart || !isVisible(element) || !window.echarts || !window.GSRCharts) {
      return;
    }
    // A chart's "Pair curves" switch (the browser may restore it checked on reload).
    var pairs = element.id && document.querySelector('[data-pairs-for="' + element.id + '"]');
    var option = window.GSRCharts.build(element.dataset.chart, readData(element.dataset.source), labels(), {
      name: element.dataset.name,
      pairs: Boolean(pairs && pairs.checked)
    });
    if (!option) {
      return;
    }
    var chart = window.echarts.init(element, window.GSRCharts.theme);
    chart.setOption(option);
    element._gsrChart = chart;
  }

  // "Merge overlaps" swaps the co-arousal episode table for the non-overlapping timeline (aria-controls names both).
  function applyMergeToggle(input) {
    var ids = (input.getAttribute("aria-controls") || "").split(" ");
    var episodes = document.getElementById(ids[0]);
    var merged = document.getElementById(ids[1]);
    if (episodes && merged) {
      episodes.hidden = input.checked;
      merged.hidden = !input.checked;
    }
  }

  function hydrate(root) {
    (root || document).querySelectorAll("[data-merge-toggle]").forEach(applyMergeToggle);
    (root || document).querySelectorAll("[data-chart]").forEach(initChart);
  }

  function disposeCharts(root) {
    if (!root || !root.querySelectorAll) {
      return;
    }
    root.querySelectorAll("[data-chart]").forEach(function (element) {
      if (element._gsrChart) {
        element._gsrChart.dispose();
        element._gsrChart = null;
      }
    });
  }

  function resizeCharts() {
    document.querySelectorAll("[data-chart]").forEach(function (element) {
      if (element._gsrChart) {
        element._gsrChart.resize();
      }
    });
  }

  // ── Analysis tabs: switch panels without a request, keep ?tab= for reloads ──
  function activateTab(name) {
    document.querySelectorAll("[data-tab]").forEach(function (link) {
      var active = link.dataset.tab === name;
      link.classList.toggle("active", active);
      link.setAttribute("aria-selected", active ? "true" : "false");
      link.tabIndex = active ? 0 : -1;
    });
    document.querySelectorAll("[data-tab-panel]").forEach(function (panel) {
      panel.hidden = panel.dataset.tabPanel !== name;
      if (!panel.hidden) {
        hydrate(panel);
      }
    });
    document.querySelectorAll("[data-tab-input]").forEach(function (input) {
      input.value = name;
    });
    var url = new URL(window.location.href);
    url.searchParams.set("tab", name);
    window.history.replaceState(null, "", url);
    // The PL/EN switch returns to this URL, so keep the selected tab across the switch.
    document.querySelectorAll('.lang-switch input[name="next"]').forEach(function (input) {
      input.value = url.pathname + url.search;
    });
    resizeCharts();
  }

  document.addEventListener("change", function (event) {
    var input = event.target;
    if (input.matches("[data-pairs-for]")) {
      var element = document.getElementById(input.dataset.pairsFor);
      if (element && element._gsrChart) {
        element._gsrChart.dispose();
        element._gsrChart = null;
      }
      if (element) {
        initChart(element);
      }
    } else if (input.matches("[data-merge-toggle]")) {
      applyMergeToggle(input);
    }
  });

  document.addEventListener("click", function (event) {
    var link = event.target.closest("[data-tab]");
    if (link) {
      event.preventDefault();
      activateTab(link.dataset.tab);
    }
  });

  // Roving focus within the tab list: Left/Right wrap around, Home/End jump to the ends.
  document.addEventListener("keydown", function (event) {
    var link = event.target.closest && event.target.closest('[role="tab"][data-tab]');
    if (!link || event.altKey || event.ctrlKey || event.metaKey) {
      return;
    }
    var tabs = Array.prototype.slice.call(link.closest('[role="tablist"]').querySelectorAll('[role="tab"][data-tab]'));
    var index = tabs.indexOf(link);
    var next = {
      ArrowRight: tabs[(index + 1) % tabs.length],
      ArrowLeft: tabs[(index - 1 + tabs.length) % tabs.length],
      Home: tabs[0],
      End: tabs[tabs.length - 1]
    }[event.key];
    if (event.key === " ") {
      next = link;
    }
    if (next) {
      event.preventDefault();
      activateTab(next.dataset.tab);
      next.focus();
    }
  });

  // ── Settings drawer: lives in #drawer-root outside #main, so the open state (a class on the root) survives the
  // HTMX swaps; each response refreshes the drawer's content out of band ──
  function drawerRoot() {
    return document.getElementById("drawer-root");
  }

  function drawerForm() {
    var root = drawerRoot();
    return root && root.querySelector(".drawer form");
  }

  function isDrawerOpen() {
    var root = drawerRoot();
    return !!root && root.classList.contains("open") && !!root.querySelector(".drawer");
  }

  function syncDrawerState() {
    var open = isDrawerOpen();
    document.body.classList.toggle("drawer-open", open);
    document.querySelectorAll("[data-open-settings]").forEach(function (button) {
      button.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  function setDrawer(open, moveFocus) {
    var root = drawerRoot();
    if (!root || !root.querySelector(".drawer")) {
      return;
    }
    root.classList.toggle("open", open);
    syncDrawerState();
    // On wide screens the work area narrows beside the drawer.
    resizeCharts();
    var target = open ? root.querySelector("#settings-drawer-title") : document.querySelector("[data-open-settings]");
    if (moveFocus && target) {
      target.focus();
    }
  }

  // A URL ending in #settings-drawer (a bookmark, a link opened in a new tab) opens the drawer through the JS state
  // instead of :target, so the close button and Escape can close it.
  function adoptDrawerHash() {
    if (window.location.hash !== "#settings-drawer") {
      return;
    }
    var url = new URL(window.location.href);
    url.hash = "";
    window.history.replaceState(window.history.state, "", url);
    setDrawer(true, false);
  }

  window.addEventListener("hashchange", adoptDrawerHash);

  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-open-settings]")) {
      event.preventDefault();
      setDrawer(!isDrawerOpen(), true);
    } else if (event.target.closest("[data-close-settings]")) {
      event.preventDefault();
      setDrawer(false, true);
    }
  });

  // ── Upload drop zone: dropped or chosen files are uploaded at once (the form's own Upload button is the
  // no-script fallback) ──
  function uploadFiles(zone, files) {
    var input = zone.querySelector('input[type="file"]');
    if (!input || !files || !files.length) {
      return;
    }
    if (input.files !== files) {
      input.files = files;
    }
    zone.closest("form").requestSubmit();
  }

  ["dragenter", "dragover"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      var zone = event.target.closest && event.target.closest("[data-dropzone]");
      if (zone && event.dataTransfer && Array.prototype.indexOf.call(event.dataTransfer.types, "Files") >= 0) {
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
        zone.classList.add("is-dragging");
      }
    });
  });
  document.addEventListener("dragleave", function (event) {
    var zone = event.target.closest && event.target.closest("[data-dropzone]");
    if (zone && !zone.contains(event.relatedTarget)) {
      zone.classList.remove("is-dragging");
    }
  });
  document.addEventListener("drop", function (event) {
    var zone = event.target.closest && event.target.closest("[data-dropzone]");
    if (zone) {
      event.preventDefault();
      zone.classList.remove("is-dragging");
      uploadFiles(zone, event.dataTransfer && event.dataTransfer.files);
    }
  });
  document.addEventListener("change", function (event) {
    var zone = event.target.matches('input[type="file"]') && event.target.closest("[data-dropzone]");
    if (zone) {
      uploadFiles(zone, event.target.files);
    }
  });

  // ── View switch (Upload raw signals: Separate | Overlay): show one panel, draw its charts on first show ──
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-view]");
    var scope = button && button.closest("[data-views]");
    if (!scope) {
      return;
    }
    scope.querySelectorAll("[data-view]").forEach(function (other) {
      other.setAttribute("aria-pressed", other === button ? "true" : "false");
    });
    scope.querySelectorAll("[data-view-panel]").forEach(function (panel) {
      panel.hidden = panel.dataset.viewPanel !== button.dataset.view;
    });
    hydrate(scope);
    resizeCharts();
  });

  // Unapplied edits: mark each edited field and show the footer note; a group switch dims its fields when off.
  function isEdited(input) {
    if (input.type === "checkbox") {
      return input.checked !== input.defaultChecked;
    }
    if (input.tagName === "SELECT") {
      return Array.prototype.some.call(input.options, function (option) {
        return option.selected !== option.defaultSelected;
      });
    }
    return input.value !== input.defaultValue;
  }

  function editableFields(form) {
    return form.querySelectorAll('input:not([type="hidden"]), select');
  }

  function markEdits(form) {
    var edited = false;
    editableFields(form).forEach(function (input) {
      var changed = isEdited(input);
      var holder = input.closest(".f, label");
      edited = edited || changed;
      if (holder) {
        holder.toggleAttribute("data-dirty", changed);
      }
      if (input.classList.contains("switch")) {
        input.closest(".group").classList.toggle("off", !input.checked);
      }
    });
    var note = form.querySelector("[data-dirty-note]");
    if (note) {
      note.hidden = !edited;
    }
  }

  ["input", "change"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      var form = event.target.closest && event.target.closest(".drawer form");
      if (form) {
        markEdits(form);
      }
    });
  });

  // Before a swap: remember the drawer's scroll position, open sections and unapplied edits, so they can be put back
  // on the refreshed drawer. Edits sent by Apply are put back only when that run failed (the refreshed drawer carries
  // data-autoopen), so the user still sees what they changed; Reset defaults discards them.
  function snapshotDrawer(source) {
    var form = drawerForm();
    if (!form) {
      return null;
    }
    var body = form.querySelector(".drawer-body");
    var fromDrawer = !!source && form.contains(source);
    var isReset = fromDrawer && /-reset$/.test(source.id || "");
    return {
      fromDrawer: fromDrawer,
      scroll: body ? body.scrollTop : 0,
      details: Array.prototype.map.call(form.querySelectorAll("details[open][id]"), function (details) {
        return details.id;
      }),
      edits: isReset ? [] : Array.prototype.filter.call(editableFields(form), isEdited).map(function (input) {
        return { name: input.name, value: input.value, checked: input.checked, type: input.type };
      })
    };
  }

  function restoreDrawer(snapshot) {
    var form = drawerForm();
    if (!form || !snapshot) {
      return;
    }
    snapshot.details.forEach(function (id) {
      var details = form.querySelector("#" + id);
      if (details) {
        details.open = true;
      }
    });
    var edits = snapshot.fromDrawer && !form.closest(".drawer[data-autoopen]") ? [] : snapshot.edits;
    edits.forEach(function (edit) {
      editableFields(form).forEach(function (input) {
        if (input.name !== edit.name) {
          return;
        }
        if (edit.type === "checkbox") {
          if (input.value === edit.value) {
            input.checked = edit.checked;
          }
        } else {
          input.value = edit.value;
        }
      });
    });
    markEdits(form);
    var body = form.querySelector(".drawer-body");
    if (body) {
      body.scrollTop = snapshot.scroll;
    }
  }

  function openDrawerOnError() {
    var root = drawerRoot();
    if (root && root.querySelector(".drawer[data-autoopen]")) {
      root.classList.add("open");
    }
    syncDrawerState();
  }

  // ── Info tooltips: placed next to their (i) button, above it when there is room, so they do not
  // cover the controls they describe; Escape hides the open one until the pointer/focus leaves it ──
  function placeTip(tip) {
    var button = tip.querySelector(".tip-btn");
    var text = tip.querySelector(".tip-text");
    if (!button || !text) {
      return;
    }
    var gap = 6;
    var margin = 8;
    var anchor = button.getBoundingClientRect();
    var box = text.getBoundingClientRect();
    var left = Math.min(Math.max(margin, anchor.left), window.innerWidth - box.width - margin);
    var top = anchor.top - box.height - gap;
    if (top < margin) {
      top = anchor.bottom + gap;
    }
    text.style.left = Math.max(margin, left) + "px";
    text.style.top = top + "px";
  }

  ["mouseover", "focusin"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      var tip = event.target.closest && event.target.closest(".tip");
      if (tip) {
        placeTip(tip);
      }
    });
  });
  window.addEventListener("scroll", function () {
    document.querySelectorAll(".tip:hover, .tip:focus-within").forEach(placeTip);
  }, true);

  // Escape hides the open tooltip first; otherwise it closes the settings drawer.
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") {
      return;
    }
    var tips = document.querySelectorAll(".tip:hover:not(.dismissed), .tip:focus-within:not(.dismissed)");
    tips.forEach(function (tip) {
      tip.classList.add("dismissed");
    });
    if (!tips.length && isDrawerOpen()) {
      setDrawer(false, true);
    }
  });
  ["mouseout", "focusout"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      var tip = event.target.closest && event.target.closest(".tip.dismissed");
      if (tip && !tip.contains(event.relatedTarget)) {
        tip.classList.remove("dismissed");
      }
    });
  });

  // ── Chart capture for the PDF report / ZIP (same builders, fixed size, white background) ──
  // The PDF form's report language picks the chart labels and number format; the ZIP uses the UI language.
  function captureLabels(form) {
    var select = form.querySelector("[data-report-lang-select]");
    var byLang = select && readData("report-chart-labels");
    return (byLang && byLang[select.value]) || labels();
  }

  function captureCharts(chartLabels) {
    var images = {};
    window.GSRCharts.locale = chartLabels.locale || null;
    try {
      document.querySelectorAll("[data-capture-chart]").forEach(function (element) {
        var option = window.GSRCharts.build(element.dataset.captureChart, readData(element.dataset.source), chartLabels, {
          capture: true,
          pairs: true
        });
        if (!option) {
          return;
        }
        var chart = window.echarts.init(element, window.GSRCharts.theme, { renderer: "canvas" });
        chart.setOption(option);
        images[element.dataset.key] = chart.getDataURL({ type: "png", pixelRatio: 2, backgroundColor: "#ffffff" });
        chart.dispose();
      });
    } finally {
      window.GSRCharts.locale = null;
    }
    return images;
  }

  // The report card's language select switches the page-1 preview and the file name.
  document.addEventListener("change", function (event) {
    var select = event.target.closest("[data-report-lang-select]");
    if (!select) {
      return;
    }
    select.closest(".report-card").querySelectorAll("[data-report-lang]").forEach(function (element) {
      element.hidden = element.dataset.reportLang !== select.value;
    });
  });

  // A file menu closes on a click outside it or on Escape.
  function closeFileMenus(except) {
    document.querySelectorAll(".file-menu[open]").forEach(function (menu) {
      if (menu !== except) {
        menu.open = false;
      }
    });
  }
  document.addEventListener("click", function (event) {
    closeFileMenus(event.target.closest(".file-menu"));
  });
  document.addEventListener("keydown", function (event) {
    var open = event.key === "Escape" && document.querySelector(".file-menu[open]");
    if (open) {
      closeFileMenus(null);
      open.querySelector("summary").focus();
    }
  });

  // "Export the outdated run anyway": stop dimming the export cards and move to the report button.
  document.addEventListener("click", function (event) {
    var link = event.target.closest("[data-export-anyway]");
    if (!link) {
      return;
    }
    event.preventDefault();
    document.querySelectorAll("[data-export-grid]").forEach(function (grid) {
      grid.classList.remove("is-stale");
    });
    var button = document.querySelector("#report-card button[type=submit]");
    if (button) {
      button.scrollIntoView({ block: "center" });
      button.focus();
    }
  });

  function setBusy(busy, button) {
    document.body.classList.toggle("is-busy", busy);
    if (button) {
      button.setAttribute("aria-busy", "true");
    }
    if (!busy) {
      clearBusyButtons();
    }
  }

  function clearBusyButtons() {
    document.querySelectorAll('button[aria-busy="true"]').forEach(function (button) {
      button.removeAttribute("aria-busy");
    });
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
      return;
    }
    if (form.hasAttribute("data-capture-charts")) {
      event.preventDefault();
      setBusy(true);
      // Let the overlay paint before the synchronous capture.
      window.setTimeout(function () {
        try {
          if (window.echarts && window.GSRCharts) {
            var images = captureCharts(captureLabels(form));
            Object.keys(images).forEach(function (key) {
              var input = form.querySelector('input[name="chart_' + key + '"]');
              if (input) {
                input.value = images[key];
              }
            });
          }
        } finally {
          form.submit();
          // The response is a download, so the page stays; clear the overlay.
          window.setTimeout(function () {
            setBusy(false);
          }, 1500);
        }
      }, 30);
      return;
    }
    if (form.hasAttribute("data-busy") && !form.hasAttribute("hx-post")) {
      setBusy(true, event.submitter);
    }
  });

  window.addEventListener("resize", resizeCharts);
  window.addEventListener("pageshow", function () {
    setBusy(false);
  });

  // The button that started a computation shows its own spinner (the overlay waits 1.5 s). A drawer button loses
  // focus while it is disabled and is then replaced, so remember it to give focus back after the swap.
  var drawerFocusId = null;
  document.addEventListener("htmx:beforeRequest", function (event) {
    var trigger = event.detail.requestConfig && event.detail.requestConfig.triggeringEvent;
    if (trigger && trigger.submitter) {
      trigger.submitter.setAttribute("aria-busy", "true");
    }
    var form = drawerForm();
    var focused = (trigger && trigger.submitter) || document.activeElement;
    drawerFocusId = form && focused && focused.id && form.contains(focused) ? focused.id : null;
  });
  document.addEventListener("htmx:afterRequest", clearBusyButtons);

  var drawerSnapshot = null;
  document.addEventListener("htmx:beforeSwap", function (event) {
    disposeCharts(event.detail.target);
    drawerSnapshot = snapshotDrawer(event.detail.requestConfig && event.detail.requestConfig.elt);
  });
  document.addEventListener("htmx:afterSwap", function () {
    restoreDrawer(drawerSnapshot);
    drawerSnapshot = null;
    openDrawerOnError();
    var focusTarget = drawerFocusId && document.getElementById(drawerFocusId);
    drawerFocusId = null;
    if (focusTarget && isDrawerOpen() && (!document.activeElement || document.activeElement === document.body)) {
      focusTarget.focus();
    }
  });
  // Swapped-in content (and out-of-band swaps) may carry new charts.
  document.addEventListener("htmx:afterSettle", function () {
    hydrate(document);
  });
  document.addEventListener("DOMContentLoaded", function () {
    adoptDrawerHash();
    openDrawerOnError();
    hydrate(document);
  });
})();
