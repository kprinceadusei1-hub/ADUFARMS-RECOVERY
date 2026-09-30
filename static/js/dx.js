/* Shared front-end helpers for the analytics-style pages (dashboard, analytics, purchases, sales, ...).
   Usage: DX.make('canvasId', chartConfig); DX.tabs({initial: 'ledger', onFirstShow: {analytics: initCharts}}). */
window.DX = (function () {
  const dark = () => document.body.dataset.theme === 'dark';
  const ink = () => (dark() ? '#c9dccf' : '#52665b');
  const grid = () => (dark() ? 'rgba(255,255,255,0.09)' : 'rgba(0,0,0,0.06)');
  const money = v => (v == null ? '—' : 'GHS ' + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  const num = v => Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 });
  const pct = v => (v == null ? '—' : (v * 100).toFixed(1) + '%');
  const palette = ['#16a34a', '#2563eb', '#d97706', '#7c3aed', '#0d9488', '#dc2626'];
  const charts = [];

  const scales = extra => Object.assign({
    x: { grid: { display: false }, ticks: { color: ink(), maxTicksLimit: 10, autoSkip: true } },
    y: { beginAtZero: true, grid: { color: grid() }, ticks: { color: ink(), callback: num } },
  }, extra || {});

  function make(id, cfg) {
    const el = document.getElementById(id);
    if (!el || !window.Chart) return null;
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    cfg.options = Object.assign({ responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false } }, cfg.options);
    cfg.options.plugins = Object.assign({ legend: { position: 'bottom', labels: { boxWidth: 10, usePointStyle: true, color: ink() } } }, cfg.options.plugins);
    const chart = new Chart(el, cfg);
    charts.push(chart);
    return chart;
  }

  function retheme() {
    charts.forEach(c => {
      Object.values(c.options.scales || {}).forEach(a => {
        if (a.ticks) a.ticks.color = ink();
        if (a.grid && a.grid.display !== false && a.grid.color !== undefined) a.grid.color = grid();
      });
      if (c.options.plugins.legend && c.options.plugins.legend.labels) c.options.plugins.legend.labels.color = ink();
      if (c.config.type === 'doughnut') c.data.datasets[0].borderColor = dark() ? '#182820' : '#ffffff';
      c.update();
    });
  }
  const toggle = document.getElementById('themeToggle');
  if (toggle) toggle.addEventListener('click', () => setTimeout(retheme, 0));
  window.addEventListener('beforeprint', () => charts.forEach(c => c.resize()));

  /* Tabs: buttons `.dx-tab[data-tab=x]` switch panels `#tab-x`. `initial` wins over the URL hash. */
  function tabs(opts) {
    opts = opts || {};
    const buttons = Array.from(document.querySelectorAll('.dx-tab'));
    const panels = {};
    buttons.forEach(b => { panels[b.dataset.tab] = document.getElementById('tab-' + b.dataset.tab); });
    const first = Object.keys(panels)[0], shown = {};
    function show(name) {
      if (!panels[name]) name = first;
      buttons.forEach(b => b.classList.toggle('active', b.dataset.tab === name));
      Object.entries(panels).forEach(([k, el]) => { el.hidden = k !== name; });
      if (!shown[name]) { shown[name] = true; if (opts.onFirstShow && opts.onFirstShow[name]) opts.onFirstShow[name](); }
      if (history.replaceState) history.replaceState(null, '', name === first ? location.pathname + location.search : '#' + name);
    }
    buttons.forEach(b => b.addEventListener('click', () => show(b.dataset.tab)));
    const hash = location.hash.replace('#', '');
    show(opts.initial || (hash === 'new' ? first : hash) || first);
    return { show };
  }

  return { dark, ink, grid, money, num, pct, palette, scales, make, tabs };
})();
