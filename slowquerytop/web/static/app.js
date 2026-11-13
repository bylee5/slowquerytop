(function () {
  "use strict";

  const PALETTE = ["#2f6fdf", "#e0702b", "#1f9d6b", "#c2417a", "#7b5cd6", "#c9a227", "#1aa3b8", "#8a6d4f",
                   "#d64545", "#5a8f29", "#3f51b5", "#b5651d", "#00897b", "#ad1457", "#546e7a", "#6d4c41",
                   "#9e9d24", "#0277bd", "#8e24aa", "#ef6c00"];

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function pad(n) { return String(n).padStart(2, "0"); }
  function localInput(d) {
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  // quick range buttons: relative to the browser clock (times are entered in the server's display tz;
  // for most installs those match)
  document.querySelectorAll(".quick button[data-hours]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const form = btn.closest("form");
      const end = new Date(Date.now() + 60000);
      const start = new Date(end.getTime() - Number(btn.dataset.hours) * 3600 * 1000);
      form.querySelector("[name=start]").value = localInput(start);
      form.querySelector("[name=end]").value = localInput(end);
      form.submit();
    });
  });

  function copyText(text, btn) {
    const done = function () {
      const old = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(function () { btn.textContent = old; }, 1200);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { window.prompt("Copy:", text); });
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); done(); } catch (e) { /* ignore */ }
      document.body.removeChild(ta);
    }
  }

  const perma = document.getElementById("permalink");
  if (perma) {
    perma.addEventListener("click", function () {
      copyText(location.origin + perma.dataset.url, perma);
    });
  }
  document.querySelectorAll("button.copy[data-target]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      copyText(document.getElementById(btn.dataset.target).textContent, btn);
    });
  });

  function readJSON(id) {
    const el = document.getElementById(id);
    return el ? JSON.parse(el.textContent) : null;
  }

  function baseOptions() {
    const grid = css("--line"), text = css("--muted");
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { labels: { color: text, boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: text, maxRotation: 0, autoSkip: true, maxTicksLimit: 12 }, grid: { color: grid } },
        y: { ticks: { color: text }, grid: { color: grid }, beginAtZero: true }
      }
    };
  }

  // report graph view
  const graph = readJSON("graph-data");
  if (graph && window.Chart) {
    const opts = baseOptions();
    opts.plugins.legend.display = graph.series.length <= 1;
    opts.spanGaps = true;
    new Chart(document.getElementById("graph"), {
      type: "line",
      data: {
        labels: graph.labels,
        datasets: graph.series.map(function (s, i) {
          return {
            label: s.label,
            data: s.data,
            borderColor: PALETTE[i % PALETTE.length],
            backgroundColor: PALETTE[i % PALETTE.length],
            borderWidth: 1.6,
            pointRadius: graph.labels.length > 60 ? 0 : 2,
            tension: 0.2
          };
        })
      },
      options: opts
    });
    document.querySelectorAll(".swatch[data-i]").forEach(function (el) {
      el.style.background = PALETTE[Number(el.dataset.i) % PALETTE.length];
    });
  }

  // query detail history: count (bars) + avg time (line, right axis)
  const hist = readJSON("history-data");
  if (hist && window.Chart) {
    const opts = baseOptions();
    opts.scales.y.title = { display: true, text: "executions", color: css("--muted") };
    opts.scales.y2 = {
      position: "right", beginAtZero: true, grid: { drawOnChartArea: false },
      ticks: { color: css("--muted") }, title: { display: true, text: "avg time (s)", color: css("--muted") }
    };
    opts.spanGaps = true;
    new Chart(document.getElementById("history"), {
      data: {
        labels: hist.labels,
        datasets: [
          { type: "bar", label: "Executions", data: hist.exec_count, backgroundColor: PALETTE[0] + "66", yAxisID: "y" },
          { type: "line", label: "Avg time (s)", data: hist.duration_avg, borderColor: PALETTE[1], backgroundColor: PALETTE[1],
            borderWidth: 1.8, pointRadius: hist.labels.length > 60 ? 0 : 2, yAxisID: "y2" }
        ]
      },
      options: opts
    });
  }
})();
