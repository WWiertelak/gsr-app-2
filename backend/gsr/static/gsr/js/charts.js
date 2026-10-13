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

  // Chart tokens (ink, lines, reserved semantic colours) from the design token set, shared by every
  // builder through the "gsr" ECharts theme. The PDF charts are captured with the same theme.
  var T = {
    ink: "#14212b",
    ink2: "#4a5a68",
    ink3: "#617080",
    line: "#cfd6dd",
    grid: "#eef1f4",
    accent: "#0b6e74",
    raw: "#c3cbd3",
    error: "#b42318",
    episode: "rgba(20,33,43,0.07)",
    episodeEdge: "rgba(20,33,43,0.28)",
    activeCount: "#3f4b57"
  };
  // Reserved science colours (services/results.py RESERVED_SCIENCE_COLORS, app.css
  // --high/--low/--comove/--threshold/--episode); participant colours come from
  // PARTICIPANT_PALETTE and never use these.
  var HIGH = "#b2182b";
  var LOW = "#2166ac";
  var NEUTRAL_STATE = "#f7f7f7";
  var THRESHOLD = "#9f1239";
  var COMOVE = "#3f4b57";
  var FONT = '"IBM Plex Sans", system-ui, -apple-system, "Segoe UI", "Helvetica Neue", Arial, sans-serif';
  var THEME = "gsr";
  var LABEL_ROW_HEIGHT = 14;

  function axisDefaults() {
    return {
      axisLine: { lineStyle: { color: T.line } },
      axisTick: { show: false },
      axisLabel: { color: T.ink3, fontSize: 11, hideOverlap: true },
      splitLine: { lineStyle: { color: T.grid } },
      nameTextStyle: { color: T.ink3, fontSize: 11 }
    };
  }

  if (window.echarts) {
    window.echarts.registerTheme(THEME, {
      backgroundColor: "transparent",
      textStyle: { fontFamily: FONT, color: T.ink2, fontSize: 11 },
      categoryAxis: axisDefaults(),
      valueAxis: axisDefaults(),
      timeAxis: axisDefaults(),
      legend: { textStyle: { color: T.ink2, fontSize: 12 } },
      tooltip: { textStyle: { color: T.ink, fontSize: 12 } },
      visualMap: { textStyle: { color: T.ink3, fontSize: 11 } }
    });
  }

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

  // Relative minutes as m:ss, like the episode tables (templatetags mmss).
  function mmss(minutes) {
    var total = Math.max(0, Math.round(minutes * 60));
    var seconds = total % 60;
    return Math.floor(total / 60) + ":" + (seconds < 10 ? "0" : "") + seconds;
  }

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
    return Object.assign({ type: "value", axisLabel: { formatter: fixed() } }, extra || {});
  }

  function minutesAxis(labels) {
    return valueAxis({ name: labels.minutes || "min", nameLocation: "middle", nameGap: 26, scale: true });
  }

  // Clock time as HH:mm (seconds only when the axis zooms below a minute); hideOverlap drops
  // labels that would run together.
  function clockAxis() {
    return {
      type: "time",
      splitNumber: 6,
      axisLabel: {
        formatter: {
          year: "{yyyy}",
          month: "{MMM}",
          day: "{d} {MMM}",
          hour: "{HH}:{mm}",
          minute: "{HH}:{mm}",
          second: "{HH}:{mm}:{ss}",
          millisecond: "{HH}:{mm}:{ss}"
        }
      }
    };
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
      label: { show: true, formatter: labels.timeWindow || "", color: T.ink2, fontSize: 10 },
      lineStyle: { type: "dashed", color: T.ink2 },
      data: lines
    };
  }

  function rawSignal(data, labels, opts) {
    return {
      animation: false,
      toolbox: toolbox(labels, opts),
      tooltip: { trigger: "axis", valueFormatter: fixed(4) },
      grid: { left: 58, right: 22, top: 26, bottom: 40 },
      xAxis: clockAxis(),
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
      toolbox: toolbox(labels, opts),
      tooltip: { trigger: "axis", valueFormatter: fixed(4) },
      legend: { type: "scroll", top: 4, left: 10, right: 60 },
      grid: { left: 58, right: 22, top: 62, bottom: 42 },
      xAxis: timeAxis ? clockAxis() : minutesAxis(labels),
      yAxis: valueAxis({ name: data.unit === "scaled" ? labels.scaled : labels.microsiemens || "µS", scale: true }),
      series: series
    };
  }

  // Rounds a value range outwards to a step of 1, 2 or 5 × 10^n so the axis ends on readable numbers.
  function niceRange(low, high) {
    var span = high - low;
    var magnitude = Math.pow(10, Math.floor(Math.log10(span / 5)));
    var step = [1, 2, 5, 10].map(function (factor) {
      return factor * magnitude;
    }).filter(function (candidate) {
      return span / candidate <= 6;
    })[0];
    return { min: Math.floor(low / step) * step, max: Math.ceil(high / step) * step };
  }

  // The y range the cleaned signal needs, with headroom of 60 % of its span on each side so raw
  // values close to it stay visible. Raw artifacts beyond it are clipped and marked at the edge.
  function cleanRange(clean) {
    var values = clean.map(function (point) {
      return point[1];
    }).filter(function (value) {
      return typeof value === "number" && isFinite(value);
    });
    if (!values.length) {
      return null;
    }
    var low = Math.min.apply(null, values);
    var high = Math.max.apply(null, values);
    var pad = (high - low) * 0.6 || Math.max(Math.abs(high) * 0.1, 0.5);
    return niceRange(low >= 0 ? Math.max(0, low - pad) : low - pad, high + pad);
  }

  // One marker per stretch of clipped samples: a sample closer than `gap` to the previous marker is skipped.
  function clipMarkers(points, range, gap) {
    var above = [];
    var below = [];
    var lastAbove = -Infinity;
    var lastBelow = -Infinity;
    points.forEach(function (point) {
      var x = point[0];
      var y = point[1];
      if (typeof y !== "number") {
        return;
      }
      if (y > range.max && x - lastAbove > gap) {
        above.push({ value: [x, range.max, y] });
        lastAbove = x;
      } else if (y < range.min && x - lastBelow > gap) {
        below.push({ value: [x, range.min, y] });
        lastBelow = x;
      }
    });
    return { above: above, below: below };
  }

  function xSpan(points) {
    if (!points.length) {
      return 0;
    }
    return points[points.length - 1][0] - points[0][0];
  }

  function clipSeries(name, markers, rotate) {
    return {
      type: "scatter",
      name: name,
      symbol: "triangle",
      symbolRotate: rotate,
      symbolSize: 8,
      symbolOffset: [0, rotate ? -4 : 4],
      clip: false,
      z: 5,
      itemStyle: { color: T.error },
      tooltip: {
        valueFormatter: function (value, dataIndex) {
          var marker = markers[dataIndex];
          return formatNumber(marker ? marker.value[2] : value, 4);
        }
      },
      data: markers
    };
  }

  // New in 2.0: raw (synced) trace in grey, cleaned trace in the signal colour, rejected samples as crosses.
  // The y axis fits the cleaned signal, so large raw artifacts don't flatten it (they are clipped at the
  // plot edge, with a triangle marking where).
  function cleanVsRaw(data, labels, opts) {
    var raw = data.raw || [];
    var rejected = data.rejected || [];
    var range = cleanRange(data.clean || []);
    var yAxis = valueAxis({ name: labels.microsiemens || "µS", scale: true });
    var clipped = { above: [], below: [] };
    var shownRejected = rejected;
    if (range) {
      yAxis.min = range.min;
      yAxis.max = range.max;
      clipped = clipMarkers(raw.concat(rejected).sort(function (a, b) {
        return a[0] - b[0];
      }), range, xSpan(raw) / 150);
      shownRejected = rejected.filter(function (point) {
        return point[1] >= range.min && point[1] <= range.max;
      });
    }
    var series = [
      {
        type: "line",
        name: labels.raw || "raw",
        showSymbol: false,
        sampling: "none",
        lineStyle: { color: T.raw, width: 1 },
        itemStyle: { color: T.raw },
        data: raw
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
        itemStyle: { color: T.error, borderColor: T.error, borderWidth: 1.4 },
        data: shownRejected
      }
    ];
    var clippedName = labels.clipped || "clipped";
    if (clipped.above.length) {
      series.push(clipSeries(clippedName, clipped.above, 0));
    }
    if (clipped.below.length) {
      series.push(clipSeries(clippedName, clipped.below, 180));
    }
    return {
      animation: false,
      toolbox: toolbox(labels, opts),
      tooltip: { trigger: "axis", valueFormatter: fixed(4) },
      legend: { type: "scroll", top: 4, left: 10, width: "80%" },
      grid: { left: 54, right: 22, top: 58, bottom: 40 },
      xAxis: minutesAxis(labels),
      yAxis: yAxis,
      series: series
    };
  }

  // Label rows for episode bands: an episode that starts before the previous label in a row has
  // cleared (overlapping episodes, or ones closer than `gap`) moves to the next row down.
  function episodeRows(episodes, gap) {
    var rowEnds = [];
    return episodes.map(function (episode) {
      var row = 0;
      while (rowEnds[row] !== undefined && rowEnds[row] + gap > episode.start) {
        row++;
      }
      rowEnds[row] = Math.max(episode.end, episode.start + gap);
      return row;
    });
  }

  // Without a `span` the bands are drawn unlabelled.
  function episodeMarkArea(episodes, span) {
    if (!episodes || !episodes.length) {
      return undefined;
    }
    // About the width of an "E12" label on a typical chart.
    var rows = span === undefined ? null : episodeRows(episodes, span * 0.04);
    return {
      silent: true,
      itemStyle: { color: T.episode, borderColor: T.episodeEdge, borderWidth: 1 },
      // Just the episode number; full membership is in the episodes table.
      label: rows
        ? { show: true, position: "insideTop", distance: 4, color: T.ink2, fontSize: 11, fontWeight: 600 }
        : { show: false },
      data: episodes.map(function (episode, index) {
        return rows
          ? [
              {
                xAxis: episode.start,
                name: "E" + (index + 1),
                label: { offset: [0, rows[index] * LABEL_ROW_HEIGHT] }
              },
              { xAxis: episode.end }
            ]
          : [{ xAxis: episode.start }, { xAxis: episode.end }];
      })
    };
  }

  // Two panels on one time axis (G1): the group mean, the threshold and the labelled episodes above, the number of
  // participants in sync below. The pair curves are hidden unless `opts.pairs` (the "Pair curves" switch).
  function syncTimeline(data, labels, opts) {
    var centers = data.centers || [];
    var span = data.span || [centers[0], centers[centers.length - 1]];
    var showPairs = Boolean(opts && opts.pairs);
    var pairSeries = (showPairs ? data.pairs || [] : []).map(function (pair, index) {
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
    var threshold = data.threshold;
    var groupSeries = {
      name: labels.groupMean,
      type: "line",
      showSymbol: false,
      smooth: true,
      z: 5,
      lineStyle: { width: 2.6, color: T.accent },
      itemStyle: { color: T.accent },
      data: centers.map(function (center, i) {
        return [center, data.groupMean[i]];
      }),
      markArea: episodeMarkArea(data.episodes, span[1] - span[0]),
      // The label sits inside the plot, above the line's right end, so it is never cut off.
      markLine: {
        silent: true,
        symbol: "none",
        lineStyle: { type: [4, 4], width: 1, color: THRESHOLD },
        label: {
          position: "insideEndTop",
          formatter: labels.threshold || "",
          color: THRESHOLD,
          fontSize: 11
        },
        data: [{ yAxis: threshold }]
      }
    };
    var activeCounts = data.activeCounts || [];
    var activeCountSeries = {
      name: labels.activeCount,
      type: "line",
      step: "middle",
      showSymbol: false,
      xAxisIndex: 1,
      yAxisIndex: 1,
      lineStyle: { width: 1, color: T.activeCount },
      itemStyle: { color: T.activeCount },
      areaStyle: { color: T.activeCount, opacity: 0.18 },
      data: centers.map(function (center, i) {
        return [center, activeCounts[i] || 0];
      })
    };
    var timeAxis = { min: span[0], max: span[1] };
    return {
      animation: false,
      toolbox: toolbox(labels, opts),
      tooltip: { trigger: "axis", valueFormatter: fixed(3) },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      legend: {
        type: "scroll",
        top: 6,
        left: 10,
        right: 72,
        data: [labels.groupMean, labels.activeCount].concat(pairSeries.map(function (series) {
          return series.name;
        }))
      },
      grid: [
        { left: 52, right: 22, top: 40, height: "58%" },
        { left: 52, right: 22, top: "76%", bottom: 44 }
      ],
      xAxis: [
        valueAxis(Object.assign({ gridIndex: 0, axisLabel: { show: false } }, timeAxis)),
        valueAxis(Object.assign({ gridIndex: 1, name: labels.minutes || "min", nameLocation: "middle", nameGap: 28 }, timeAxis))
      ],
      yAxis: [
        valueAxis({ gridIndex: 0, min: -1, max: 1, interval: 0.5 }),
        valueAxis({
          gridIndex: 1,
          min: 0,
          max: Math.max(data.aliasCount || 0, 2),
          minInterval: 1,
          splitNumber: 2,
          name: labels.inSync || "",
          nameLocation: "middle",
          nameGap: 30
        })
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
    series[0].markArea = episodeMarkArea(data.episodes, xSpan(series[0].data));
    return {
      animation: false,
      toolbox: toolbox(labels, opts),
      tooltip: { trigger: "axis", valueFormatter: fixed(4) },
      legend: { type: "scroll", top: 6, left: 10, right: 60 },
      grid: { left: 52, right: 22, top: 60, bottom: 48 },
      xAxis: valueAxis({ name: labels.minutes || "min", nameLocation: "middle", nameGap: 28 }),
      yAxis: valueAxis({ name: labels.scaled }),
      series: series
    };
  }

  // ── Heatmap cell labels: ink or white, whichever contrasts more with the cell colour ──
  var HEATMAP_COLORS = [LOW, "#92c5de", NEUTRAL_STATE, "#f4a582", HIGH];

  function hexToRgb(hex) {
    return [1, 3, 5].map(function (offset) {
      return parseInt(hex.substr(offset, 2), 16);
    });
  }

  // The colour visualMap paints for `value` in [min, max] over evenly spaced `stops`.
  function rampColor(value, min, max, stops) {
    var position = Math.min(Math.max((value - min) / (max - min), 0), 1) * (stops.length - 1);
    var index = Math.min(Math.floor(position), stops.length - 2);
    var from = hexToRgb(stops[index]);
    var to = hexToRgb(stops[index + 1]);
    return from.map(function (channel, i) {
      return channel + (to[i] - channel) * (position - index);
    });
  }

  function luminance(rgb) {
    var linear = rgb.map(function (channel) {
      var c = channel / 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
  }

  function labelColorOn(rgb) {
    var cell = luminance(rgb);
    var ink = luminance(hexToRgb(T.ink));
    var onWhite = 1.05 / (cell + 0.05);
    var onInk = (cell + 0.05) / (ink + 0.05);
    return onWhite > onInk ? "#ffffff" : T.ink;
  }

  function heatmap(data, labels, opts) {
    var matrix = data.matrix || [];
    var aliases = data.aliases || [];
    var cells = [];
    matrix.forEach(function (row, rowIndex) {
      row.forEach(function (value, colIndex) {
        var cell = { value: [colIndex, rowIndex, value] };
        if (typeof value === "number") {
          cell.label = { color: labelColorOn(rampColor(value, -1, 1, HEATMAP_COLORS)) };
        }
        cells.push(cell);
      });
    });
    return {
      animation: false,
      toolbox: toolbox(labels, opts),
      tooltip: {
        position: "top",
        formatter: function (params) {
          var value = params && Array.isArray(params.value) ? params.value[2] : undefined;
          return formatNumber(value, 3);
        }
      },
      // Top margin keeps the PNG button clear of the first row of cells.
      grid: { left: 92, right: 20, top: 36, bottom: 118 },
      xAxis: { type: "category", data: aliases, axisLabel: { rotate: 20, color: T.ink2, fontSize: 12 }, splitLine: { show: false } },
      yAxis: { type: "category", data: aliases, axisLabel: { color: T.ink2, fontSize: 12 }, splitLine: { show: false } },
      visualMap: {
        min: -1,
        max: 1,
        calculable: true,
        formatter: fixed(2),
        orient: "horizontal",
        left: "center",
        bottom: 14,
        inRange: { color: HEATMAP_COLORS }
      },
      series: [
        {
          type: "heatmap",
          data: cells,
          itemStyle: { borderColor: "#ffffff", borderWidth: 1 },
          label: {
            show: true,
            fontWeight: 600,
            formatter: function (params) {
              return Array.isArray(params.value) ? formatNumber(params.value[2], 2) : "";
            }
          }
        }
      ]
    };
  }

  var TRACK_LANE_HEIGHT = 22;

  function isWhole(value) {
    return Math.abs(value - Math.round(value)) < 1e-6;
  }

  function episodeColor(direction) {
    return direction === "High" ? HIGH : direction === "Low" ? LOW : COMOVE;
  }

  function episodeLabel(direction, labels) {
    return direction === "High" ? labels.high : direction === "Low" ? labels.low : labels.comove;
  }

  // One co-arousal episode on the track: a bar in the reserved state colour, labelled when it is wide enough.
  function renderEpisode(episodes, labels) {
    return function (params, api) {
      var episode = episodes[params.dataIndex];
      var start = api.coord([api.value(0), api.value(2)]);
      var end = api.coord([api.value(1), api.value(2)]);
      var height = api.size([0, 1])[1] * 0.78;
      var width = Math.max(end[0] - start[0], 2);
      var text = episodeLabel(episode.direction, labels) || "";
      var children = [
        {
          type: "rect",
          shape: { x: start[0], y: start[1] - height / 2, width: width, height: height, r: 3 },
          style: { fill: episodeColor(episode.direction) }
        }
      ];
      // About 6.5 px per character at 11 px.
      if (text && width > text.length * 6.5 + 10) {
        children.push({
          type: "text",
          style: {
            text: text,
            x: start[0] + 6,
            y: start[1],
            verticalAlign: "middle",
            fill: "#ffffff",
            fontSize: 11,
            fontWeight: 600,
            fontFamily: FONT
          }
        });
      }
      return { type: "group", children: children };
    };
  }

  // The state map (1-minute bins per participant) with the co-arousal episodes on a track above it (G5). The heatmap
  // needs category axes, so both panels also get a value axis in minutes spanning the same bins.
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
    var episodes = data.episodes || [];
    var lanes = data.lanes || 0;
    var binWidth = times.length > 1 ? (times[times.length - 1] - times[0]) / (times.length - 1) : 1;
    var minutes = { min: times[0], max: times[0] + binWidth * times.length };
    var trackHeight = lanes * TRACK_LANE_HEIGHT;
    var mapTop = 36 + (lanes ? trackHeight + 12 : 0);
    var laneNames = [];
    for (var lane = 0; lane < lanes; lane++) {
      laneNames.push(String(lane));
    }
    var option = {
      animation: false,
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
      grid: [{ left: 92, right: 20, top: mapTop, bottom: 116 }],
      xAxis: [
        { type: "category", gridIndex: 0, data: times.map(String), show: false },
        valueAxis(Object.assign({
          gridIndex: 0,
          position: "bottom",
          name: labels.timeMin,
          nameLocation: "middle",
          nameGap: 30,
          splitLine: { show: false },
          axisLine: { show: true, lineStyle: { color: T.line } },
          // The bins rarely start on a whole minute: label only the whole-minute ticks.
          axisLabel: {
            formatter: fixed(),
            showMinLabel: isWhole(minutes.min),
            showMaxLabel: isWhole(minutes.max)
          }
        }, minutes))
      ],
      // First participant on top, as in the tables.
      yAxis: [{
        type: "category",
        gridIndex: 0,
        inverse: true,
        data: aliases,
        axisLabel: { color: T.ink2, fontSize: 12 },
        splitLine: { show: false }
      }],
      visualMap: {
        min: -1,
        max: 1,
        calculable: true,
        seriesIndex: 0,
        formatter: fixed(2),
        orient: "horizontal",
        left: "center",
        bottom: 4,
        text: [labels.high, labels.low],
        inRange: { color: [LOW, NEUTRAL_STATE, HIGH] }
      },
      series: [{ type: "heatmap", xAxisIndex: 0, yAxisIndex: 0, data: cells }]
    };
    if (lanes) {
      option.grid.push({ left: 92, right: 20, top: 36, height: trackHeight });
      option.xAxis.push(valueAxis(Object.assign({ gridIndex: 1, show: false }, minutes)));
      option.yAxis.push({
        type: "category",
        gridIndex: 1,
        inverse: true,
        data: laneNames,
        axisLine: { show: false },
        splitLine: { show: false },
        axisLabel: {
          color: T.ink2,
          fontSize: 12,
          formatter: function (value, index) {
            return index === 0 ? labels.episodes || "" : "";
          }
        }
      });
      option.series.push({
        type: "custom",
        xAxisIndex: 2,
        yAxisIndex: 1,
        encode: { x: [0, 1], y: 2 },
        renderItem: renderEpisode(episodes, labels),
        tooltip: {
          formatter: function (params) {
            var episode = episodes[params.dataIndex];
            return (
              escapeHtml(episodeLabel(episode.direction, labels) || "") + " · " +
              mmss(episode.start) + "–" + mmss(episode.end) +
              (episode.contributors.length ? "<br/>" + escapeHtml(episode.contributors.join(", ")) : "")
            );
          }
        },
        data: episodes.map(function (episode) {
          return [episode.start, episode.end, String(episode.lane)];
        })
      });
    }
    return option;
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
    kinds: Object.keys(BUILDERS),
    // Pass to echarts.init so the chart (on screen or captured for the PDF) uses the shared theme.
    theme: THEME
  };
})();
