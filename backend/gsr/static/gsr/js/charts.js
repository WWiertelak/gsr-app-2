/*
 * ECharts option builders, ported from GSRv2 (frontend/src/App.tsx
 * buildSynchronyTimelineOption / buildEpisodeSignalsOption /
 * buildContagionStateHeatmapOption, renderRawPlots / renderProcessedPlots, and
 * charts/options.ts buildHeatmapOption). The segment-coverage chart is not ported.
 *
 * GSRCharts.build(kind, data, labels, opts) returns an ECharts option; `data`
 * comes from gsr/charts.py, `labels` from chart_labels() (current UI language).
 */
(function () {
  "use strict";

  var AXIS_LINE = { lineStyle: { color: "#8ea8bb" } };
  var AXIS_LABEL = { color: "#486581" };
  var SPLIT_LINE = { lineStyle: { color: "#dce8f0" } };
  var NAME_STYLE = { color: "#486581", fontWeight: 500 };
  var BACKGROUND = "#f8fcfb";
  // Reserved science colours (services/results.py RESERVED_SCIENCE_COLORS, app.css
  // --high/--low/--comove/--threshold/--episode); participant colours come from
  // PARTICIPANT_PALETTE and never use these.
  var HIGH = "#b2182b";
  var LOW = "#2166ac";
  var NEUTRAL_STATE = "#f7f7f7";
  var COMOVE = "#3f4b57";
  var GROUP_MEAN = "#0b6e74";
  var THRESHOLD = "#b45309";
  var REJECTED = "#b42318";
  var EPISODE_AREA_COLOR = "rgba(20,33,43,0.07)";
  var EPISODE_LABEL_COLOR = "#4a5a68";
  // Pair curves are pairs, not participants: neutral greys.
  var PAIR_COLORS = ["#9aa5b1", "#aab4bf", "#8996a3", "#b7c0c9", "#7f8c99", "#a3adb8"];

  function pairColor(index) {
    return PAIR_COLORS[index % PAIR_COLORS.length];
  }

  // The participant palette repeats from the 9th signal; dash a colour already used in this chart.
  function participantLineType(color, seen) {
    if (seen[color]) {
      return "dashed";
    }
    seen[color] = true;
    return "solid";
  }

  // Numbers in the page language (document <html lang>), so PL charts show "0,57" like the tables.
  var numberFormats = {};

  function formatNumber(value, digits) {
    if (typeof value !== "number" || !isFinite(value)) {
      return "";
    }
    var key = digits === undefined ? "auto" : String(digits);
    if (!numberFormats[key]) {
      var options = digits === undefined
        ? { maximumFractionDigits: 6, useGrouping: false }
        : { minimumFractionDigits: digits, maximumFractionDigits: digits, useGrouping: false };
      try {
        numberFormats[key] = new Intl.NumberFormat(document.documentElement.lang || undefined, options);
      } catch (error) {
        numberFormats[key] = new Intl.NumberFormat(undefined, options);
      }
    }
    return numberFormats[key].format(value);
  }

  function fixed(digits) {
    return function (value) {
      return formatNumber(value, digits);
    };
  }

  var LOCALE_AXIS_LABEL = Object.assign({}, AXIS_LABEL, { formatter: fixed() });

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function toolbox(labels, opts) {
    if (opts && opts.capture) {
      return undefined;
    }
    return {
      right: 6,
      top: 0,
      feature: {
        saveAsImage: {
          title: labels.savePng || "PNG",
          name: (opts && opts.name) || "chart",
          pixelRatio: 2,
          backgroundColor: "#ffffff"
        }
      }
    };
  }

  function valueAxis(extra) {
    return Object.assign(
      { type: "value", axisLine: AXIS_LINE, axisLabel: LOCALE_AXIS_LABEL, splitLine: SPLIT_LINE, nameTextStyle: NAME_STYLE },
      extra || {}
    );
  }

  function minutesAxis(labels) {
    return valueAxis({ name: labels.minutes || "min", nameLocation: "middle", nameGap: 26, scale: true });
  }

  // Dashed vertical lines at the configured time window (cleaning range), if set.
  function windowMarkLine(window, labels) {
    if (!window) {
      return undefined;
    }
    var lines = [];
    if (typeof window.from === "number") {
      lines.push({ xAxis: window.from });
    }
    if (typeof window.to === "number") {
      lines.push({ xAxis: window.to });
    }
    if (!lines.length) {
      return undefined;
    }
    return {
      silent: true,
      symbol: "none",
      label: { show: true, formatter: labels.timeWindow || "", color: "#486581", fontSize: 10 },
      lineStyle: { type: "dashed", color: "#486581" },
      data: lines
    };
  }

  function rawSignal(data, labels, opts) {
    return {
      animation: false,
      backgroundColor: BACKGROUND,
      toolbox: toolbox(labels, opts),
      tooltip: { trigger: "axis", valueFormatter: fixed(4) },
      grid: { left: 58, right: 22, top: 26, bottom: 40 },
      xAxis: { type: "time", axisLine: AXIS_LINE, axisLabel: AXIS_LABEL, splitLine: SPLIT_LINE },
      yAxis: valueAxis({ name: labels.microsiemens || "µS", scale: true }),
      series: [
        {
          type: "line",
          showSymbol: false,
          sampling: "none",
          lineStyle: { color: data.color, width: 1.9 },
          itemStyle: { color: data.color },
          data: data.points
        }
      ]
    };
  }

  function overlay(data, labels, opts) {
    var timeAxis = data.axis === "time";
    var seen = {};
    var series = (data.series || []).map(function (entry, index) {
      return {
        type: "line",
        name: entry.alias,
        showSymbol: false,
        sampling: "none",
        lineStyle: { color: entry.color, width: timeAxis ? 1.8 : 2, type: participantLineType(entry.color, seen) },
        itemStyle: { color: entry.color },
        data: entry.points,
        markLine: index === 0 && !timeAxis ? windowMarkLine(data.window, labels) : undefined
      };
    });
    return {
      animation: false,
      backgroundColor: BACKGROUND,
      toolbox: toolbox(labels, opts),
      tooltip: { trigger: "axis", valueFormatter: fixed(4) },
      legend: { type: "scroll", top: 4, left: 10, right: 60, textStyle: { color: "#486581" } },
      grid: { left: 58, right: 22, top: 62, bottom: 42 },
      xAxis: timeAxis
        ? { type: "time", axisLine: AXIS_LINE, axisLabel: AXIS_LABEL, splitLine: SPLIT_LINE }
        : minutesAxis(labels),
      yAxis: valueAxis({ name: data.unit === "scaled" ? labels.scaled : labels.microsiemens || "µS", scale: true }),
      series: series
    };
  }

  // New in 2.0: raw (synced) trace in grey, cleaned trace in the signal colour, rejected samples as crosses.
  function cleanVsRaw(data, labels, opts) {
    return {
      animation: false,
      backgroundColor: BACKGROUND,
      toolbox: toolbox(labels, opts),
      tooltip: { trigger: "axis", valueFormatter: fixed(4) },
      legend: { top: 4, left: 10, right: 60, textStyle: { color: "#486581" } },
      grid: { left: 54, right: 22, top: 58, bottom: 40 },
      xAxis: minutesAxis(labels),
      yAxis: valueAxis({ name: labels.microsiemens || "µS", scale: true }),
      series: [
        {
          type: "line",
          name: labels.raw || "raw",
          showSymbol: false,
          sampling: "none",
          lineStyle: { color: "#b8c4ce", width: 1 },
          itemStyle: { color: "#b8c4ce" },
          data: data.raw || []
        },
        {
          type: "line",
          name: labels.clean || "clean",
          showSymbol: false,
          sampling: "none",
          lineStyle: { color: data.color, width: 2.1 },
          itemStyle: { color: data.color },
          data: data.clean || [],
          markLine: windowMarkLine(data.window, labels)
        },
        {
          type: "scatter",
          name: labels.rejected || "rejected",
          symbol: "path://M0,0 L10,10 M10,0 L0,10",
          symbolSize: 7,
          itemStyle: { color: REJECTED, borderColor: REJECTED, borderWidth: 1.4 },
          data: data.rejected || []
        }
      ]
    };
  }

  function episodeMarkArea(episodes, withLabel) {
    if (!episodes || !episodes.length) {
      return undefined;
    }
    return {
      silent: true,
      label: { show: false },
      data: episodes.map(function (episode, index) {
        return [
          {
            xAxis: episode.start,
            name: "#" + (index + 1),
            itemStyle: { color: EPISODE_AREA_COLOR },
            // Just the episode number, alternating top/bottom so adjacent labels
            // don't overlap. Full membership is in the episodes table.
            label: withLabel
              ? {
                  show: true,
                  position: index % 2 === 0 ? "insideTop" : "insideBottom",
                  color: EPISODE_LABEL_COLOR,
                  fontSize: 10,
                  formatter: "#" + (index + 1)
                }
              : { show: false }
          },
          { xAxis: episode.end }
        ];
      })
    };
  }

  function syncTimeline(data, labels, opts) {
    var centers = data.centers || [];
    var pairSeries = (data.pairs || []).map(function (pair, index) {
      return {
        name: pair.name,
        type: "line",
        showSymbol: false,
        smooth: true,
        lineStyle: { width: 1, color: pairColor(index) },
        itemStyle: { color: pairColor(index) },
        data: centers.map(function (center, i) {
          return [center, pair.values[i]];
        })
      };
    });
    var groupSeries = {
      name: labels.groupMean,
      type: "line",
      showSymbol: false,
      smooth: true,
      lineStyle: { width: 3, color: GROUP_MEAN },
      itemStyle: { color: GROUP_MEAN },
      data: centers.map(function (center, i) {
        return [center, data.groupMean[i]];
      }),
      markLine: {
        silent: true,
        symbol: "none",
        lineStyle: { type: "dashed", color: THRESHOLD },
        label: { formatter: labels.threshold || "", color: THRESHOLD },
        data: [{ yAxis: data.threshold }]
      }
    };
    var activeCounts = data.activeCounts || [];
    var activeCountSeries = {
      name: labels.activeCount,
      type: "line",
      step: "middle",
      showSymbol: false,
      yAxisIndex: 1,
      lineStyle: { width: 1.5, color: COMOVE },
      itemStyle: { color: COMOVE },
      data: centers.map(function (center, i) {
        return [center, activeCounts[i] || 0];
      }),
      markArea: episodeMarkArea(data.episodes, false)
    };
    return {
      animation: false,
      backgroundColor: BACKGROUND,
      toolbox: toolbox(labels, opts),
      tooltip: { trigger: "axis", valueFormatter: fixed(3) },
      legend: { type: "scroll", top: 6, left: 10, right: 60, textStyle: { color: "#486581" } },
      grid: { left: 52, right: 46, top: 44, bottom: 48 },
      xAxis: { type: "value", name: labels.minutes || "min", nameLocation: "middle", nameGap: 28, axisLabel: { formatter: fixed() }, splitLine: SPLIT_LINE },
      yAxis: [
        { type: "value", min: -1, max: 1, axisLabel: { formatter: fixed() }, splitLine: SPLIT_LINE },
        {
          type: "value",
          min: 0,
          max: Math.max(data.aliasCount || 0, 2),
          interval: 1,
          axisLabel: { color: COMOVE },
          splitLine: { show: false }
        }
      ],
      series: pairSeries.concat([groupSeries, activeCountSeries])
    };
  }

  function episodeSignals(data, labels, opts) {
    var seen = {};
    var series = (data.series || [])
      .filter(function (entry) {
        return entry.points && entry.points.length > 0;
      })
      .map(function (entry) {
        return {
          type: "line",
          name: entry.alias,
          showSymbol: false,
          sampling: "lttb",
          lineStyle: { width: 1.5, color: entry.color, type: participantLineType(entry.color, seen) },
          itemStyle: { color: entry.color },
          data: entry.points
        };
      });
    if (!series.length) {
      return null;
    }
    series[0].markArea = episodeMarkArea(data.episodes, true);
    return {
      animation: false,
      backgroundColor: BACKGROUND,
      toolbox: toolbox(labels, opts),
      tooltip: { trigger: "axis", valueFormatter: fixed(4) },
      legend: { type: "scroll", top: 6, left: 10, right: 60, textStyle: { color: "#486581" } },
      grid: { left: 52, right: 22, top: 60, bottom: 48 },
      xAxis: { type: "value", name: labels.minutes || "min", nameLocation: "middle", nameGap: 28, axisLabel: { formatter: fixed() }, splitLine: SPLIT_LINE },
      yAxis: { type: "value", name: labels.scaled, axisLabel: { formatter: fixed() }, splitLine: SPLIT_LINE },
      series: series
    };
  }

  function heatmap(data, labels, opts) {
    var matrix = data.matrix || [];
    var aliases = data.aliases || [];
    var cells = [];
    matrix.forEach(function (row, rowIndex) {
      row.forEach(function (value, colIndex) {
        cells.push([colIndex, rowIndex, value]);
      });
    });
    return {
      animation: false,
      backgroundColor: BACKGROUND,
      toolbox: toolbox(labels, opts),
      tooltip: {
        position: "top",
        formatter: function (params) {
          var value = params && Array.isArray(params.value) ? params.value[2] : undefined;
          return formatNumber(value, 3);
        }
      },
      grid: { left: 92, right: 20, top: 24, bottom: 118 },
      xAxis: { type: "category", data: aliases, axisLabel: { rotate: 20, color: "#486581" }, axisLine: AXIS_LINE },
      yAxis: { type: "category", data: aliases, axisLabel: AXIS_LABEL, axisLine: AXIS_LINE },
      visualMap: {
        min: -1,
        max: 1,
        calculable: true,
        formatter: fixed(2),
        orient: "horizontal",
        left: "center",
        bottom: 14,
        inRange: { color: [LOW, "#92c5de", NEUTRAL_STATE, "#f4a582", HIGH] }
      },
      series: [
        {
          type: "heatmap",
          data: cells,
          label: {
            show: true,
            color: "#13315c",
            formatter: function (params) {
              return Array.isArray(params.value) ? formatNumber(params.value[2], 2) : "";
            }
          }
        }
      ]
    };
  }

  function coarousalStates(data, labels, opts) {
    var rows = data.rows || [];
    var times = data.times || [];
    if (!rows.length || !times.length) {
      return null;
    }
    var aliases = rows.map(function (row) {
      return row.alias;
    });
    var cells = [];
    rows.forEach(function (row, rowIndex) {
      row.states.forEach(function (value, colIndex) {
        cells.push([colIndex, rowIndex, Number(value.toFixed(3))]);
      });
    });
    return {
      animation: false,
      backgroundColor: BACKGROUND,
      toolbox: toolbox(labels, opts),
      tooltip: {
        position: "top",
        formatter: function (params) {
          var value = params && Array.isArray(params.value) ? params.value : null;
          if (!value) {
            return "";
          }
          var time = times[value[0]];
          return (
            escapeHtml(aliases[value[1]]) + " · " + formatNumber(time, 1) + " min<br/>" +
            escapeHtml(labels.state || "") + ": " + formatNumber(Number(value[2]), 2)
          );
        }
      },
      grid: { left: 92, right: 20, top: 24, bottom: 104 },
      xAxis: {
        type: "category",
        data: times.map(function (value) {
          return formatNumber(value, 0);
        }),
        name: labels.timeMin,
        nameLocation: "middle",
        nameGap: 30,
        axisLabel: { color: "#486581", interval: Math.max(0, Math.floor(times.length / 20)) },
        axisLine: AXIS_LINE
      },
      yAxis: { type: "category", data: aliases, axisLabel: AXIS_LABEL, axisLine: AXIS_LINE },
      visualMap: {
        min: -1,
        max: 1,
        calculable: true,
        formatter: fixed(2),
        orient: "horizontal",
        left: "center",
        bottom: 4,
        text: [labels.high, labels.low],
        inRange: { color: [LOW, NEUTRAL_STATE, HIGH] }
      },
      series: [{ type: "heatmap", data: cells }]
    };
  }

  var BUILDERS = {
    rawSignal: rawSignal,
    overlay: overlay,
    cleanVsRaw: cleanVsRaw,
    syncTimeline: syncTimeline,
    episodeSignals: episodeSignals,
    heatmap: heatmap,
    coarousalStates: coarousalStates
  };

  window.GSRCharts = {
    build: function (kind, data, labels, opts) {
      var builder = BUILDERS[kind];
      if (!builder || !data) {
        return null;
      }
      return builder(data, labels || {}, opts || {});
    },
    kinds: Object.keys(BUILDERS)
  };
})();
