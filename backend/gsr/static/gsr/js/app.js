/*
 * Page behaviour: hydrate [data-chart] elements with ECharts (also after HTMX
 * swaps), analysis tabs (ARIA tabs with arrow keys), info tooltips, confirm dialogs, the busy overlay for plain form posts,
 * and the Export screen's chart capture for the PDF report and the ZIP.
 */
(function () {
  "use strict";

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
    var option = window.GSRCharts.build(element.dataset.chart, readData(element.dataset.source), labels(), {
      name: element.dataset.name
    });
    if (!option) {
      return;
    }
    var chart = window.echarts.init(element, window.GSRCharts.theme);
    chart.setOption(option);
    element._gsrChart = chart;
  }

  function hydrate(root) {
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

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      document.querySelectorAll(".tip:hover, .tip:focus-within").forEach(function (tip) {
        tip.classList.add("dismissed");
      });
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
  function captureCharts() {
    var images = {};
    document.querySelectorAll("[data-capture-chart]").forEach(function (element) {
      var option = window.GSRCharts.build(element.dataset.captureChart, readData(element.dataset.source), labels(), {
        capture: true
      });
      if (!option) {
        return;
      }
      var chart = window.echarts.init(element, window.GSRCharts.theme, { renderer: "canvas" });
      chart.setOption(option);
      images[element.dataset.key] = chart.getDataURL({ type: "png", pixelRatio: 2, backgroundColor: "#ffffff" });
      chart.dispose();
    });
    return images;
  }

  function setBusy(busy) {
    document.body.classList.toggle("is-busy", busy);
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
            var images = captureCharts();
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
      setBusy(true);
    }
  });

  window.addEventListener("resize", resizeCharts);
  window.addEventListener("pageshow", function () {
    setBusy(false);
  });

  document.addEventListener("htmx:beforeSwap", function (event) {
    disposeCharts(event.detail.target);
  });
  // Swapped-in content (and out-of-band swaps) may carry new charts.
  document.addEventListener("htmx:afterSettle", function () {
    hydrate(document);
  });
  document.addEventListener("DOMContentLoaded", function () {
    hydrate(document);
  });
})();
