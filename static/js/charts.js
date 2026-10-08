// Small dependency-free SVG charts for the Dashboard. Each chart is a
// <div class="viz" data-chart-id="..."> holding a <script type="application/json">
// spec built by _dashboard_charts() in foxdesk/services/reports.py:
//   {type: 'columns'|'hbar', stacked?, money?, unit?, series: [{name}], rows: [{label, tip?, values: [...]}]}
// Marks follow the dashboard chart spec: ≤24px bars with a 4px rounded data
// end and square baseline, 2px surface gaps between stacked segments,
// hairline grid, text in ink colors (never the series color), and a per-mark
// hover/focus tooltip. Every value is also in the <details> table below.
(function () {
  const NS = 'http://www.w3.org/2000/svg';
  const SERIES_VARS = ['--viz-series-1', '--viz-series-2'];
  let tooltip;

  function el(name, attrs, parent) {
    const node = document.createElementNS(NS, name);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (parent) parent.appendChild(node);
    return node;
  }

  function fmt(value, spec) {
    if (spec.money) return '$' + value.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 });
    return value.toLocaleString();
  }

  function niceMax(max) {
    if (max <= 0) return { top: 1, step: 1 };
    const rough = max / 4;
    const mag = Math.pow(10, Math.floor(Math.log10(rough)));
    const step = [1, 2, 5, 10].map(function (m) { return m * mag; }).find(function (s) { return s >= rough; });
    const finalStep = Math.max(step, 1);
    return { top: Math.ceil(max / finalStep) * finalStep, step: finalStep };
  }

  // A bar with a 4px rounded data end and a square baseline end.
  function barPath(x, y, w, h, roundAt) {
    const r = Math.min(4, roundAt === 'top' ? w / 2 : h / 2, roundAt === 'top' ? h : w);
    if (r <= 0 || w <= 0 || h <= 0) return 'M' + x + ',' + y + 'h' + Math.max(w, 0) + 'v' + Math.max(h, 0) + 'h' + -Math.max(w, 0) + 'Z';
    if (roundAt === 'top') {
      return 'M' + x + ',' + (y + h) + 'V' + (y + r) + 'Q' + x + ',' + y + ' ' + (x + r) + ',' + y +
        'H' + (x + w - r) + 'Q' + (x + w) + ',' + y + ' ' + (x + w) + ',' + (y + r) + 'V' + (y + h) + 'Z';
    }
    return 'M' + x + ',' + y + 'H' + (x + w - r) + 'Q' + (x + w) + ',' + y + ' ' + (x + w) + ',' + (y + r) +
      'V' + (y + h - r) + 'Q' + (x + w) + ',' + (y + h) + ' ' + (x + w - r) + ',' + (y + h) + 'H' + x + 'Z';
  }

  function showTip(evt, valueText, labelText, detailText) {
    if (!tooltip) {
      tooltip = document.createElement('div');
      tooltip.className = 'viz-tooltip';
      document.body.appendChild(tooltip);
    }
    tooltip.replaceChildren();
    const v = document.createElement('strong'); v.textContent = valueText; tooltip.appendChild(v);
    const l = document.createElement('div'); l.textContent = labelText; tooltip.appendChild(l);
    if (detailText) { const d = document.createElement('div'); d.className = 'viz-tooltip-detail'; d.textContent = detailText; tooltip.appendChild(d); }
    tooltip.style.display = 'block';
    let x, y;
    if (evt.clientX !== undefined && evt.type !== 'focus') { x = evt.clientX; y = evt.clientY; }
    else { const r = evt.target.getBoundingClientRect(); x = r.left + r.width / 2; y = r.top; }
    const tw = tooltip.offsetWidth, th = tooltip.offsetHeight;
    tooltip.style.left = Math.min(Math.max(8, x - tw / 2), window.innerWidth - tw - 8) + 'px';
    tooltip.style.top = (y - th - 12 < 8 ? y + 16 : y - th - 12) + 'px';
  }
  function hideTip() { if (tooltip) tooltip.style.display = 'none'; }

  function wireHit(hit, group, valueText, labelText, detailText) {
    hit.setAttribute('tabindex', '0');
    hit.setAttribute('role', 'img');
    hit.setAttribute('aria-label', labelText + ': ' + valueText + (detailText ? ' (' + detailText + ')' : ''));
    function on(e) { group.classList.add('viz-hover'); showTip(e, valueText, labelText, detailText); }
    function off() { group.classList.remove('viz-hover'); hideTip(); }
    hit.addEventListener('pointermove', on);
    hit.addEventListener('focus', on);
    hit.addEventListener('pointerleave', off);
    hit.addEventListener('blur', off);
  }

  function renderColumns(svg, spec, width) {
    const height = 210, left = 34, right = 8, top = 12, bottom = 24;
    svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
    svg.setAttribute('height', height);
    const values = spec.rows.map(function (r) { return r.values[0]; });
    const scale = niceMax(Math.max.apply(null, values));
    const plotW = width - left - right, plotH = height - top - bottom;
    for (let t = 0; t <= scale.top; t += scale.step) {
      const y = top + plotH - (t / scale.top) * plotH;
      el('line', { x1: left, x2: width - right, y1: y, y2: y, class: t === 0 ? 'viz-axis' : 'viz-gridline' }, svg);
      el('text', { x: left - 6, y: y + 4, class: 'viz-tick', 'text-anchor': 'end' }, svg).textContent = fmt(t, spec);
    }
    const band = plotW / spec.rows.length;
    const barW = Math.min(24, band * 0.6);
    const labelEvery = band < 34 ? Math.ceil(34 / band) : 1;
    spec.rows.forEach(function (row, i) {
      const v = row.values[0];
      const h = (v / scale.top) * plotH;
      const cx = left + band * i + band / 2;
      const g = el('g', { class: 'viz-mark' }, svg);
      if (v > 0) el('path', { d: barPath(cx - barW / 2, top + plotH - h, barW, h, 'top'), fill: 'var(' + SERIES_VARS[0] + ')' }, g);
      if ((spec.rows.length - 1 - i) % labelEvery === 0) {
        el('text', { x: cx, y: height - 6, class: 'viz-tick', 'text-anchor': 'middle' }, svg).textContent = row.label;
      }
      const hit = el('rect', { x: left + band * i, y: top, width: band, height: plotH, class: 'viz-hit' }, g);
      const unit = spec.unit ? ' ' + spec.unit + (v === 1 ? '' : 's') : '';
      wireHit(hit, g, fmt(v, spec) + unit, row.tip || row.label);
    });
    // Label only the latest value — the axis and tooltip carry the rest.
    const last = spec.rows.length - 1, lv = values[last];
    const ly = top + plotH - (lv / scale.top) * plotH - 6;
    el('text', { x: left + band * last + band / 2, y: ly, class: 'viz-value', 'text-anchor': 'middle' }, svg).textContent = fmt(lv, spec);
  }

  function renderHbar(svg, spec, width) {
    const rowH = 44, labelH = 16, barH = 14, right = 64, top = 4;
    const height = top + spec.rows.length * rowH;
    svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
    svg.setAttribute('height', height);
    const totals = spec.rows.map(function (r) { return r.values.reduce(function (a, b) { return a + b; }, 0); });
    const max = Math.max.apply(null, totals.concat([1]));
    const plotW = width - right;
    el('line', { x1: 0.5, x2: 0.5, y1: top, y2: height, class: 'viz-axis' }, svg);
    spec.rows.forEach(function (row, i) {
      const y = top + i * rowH;
      el('text', { x: 0, y: y + labelH - 3, class: 'viz-label' }, svg).textContent = row.label;
      const barY = y + labelH + 4;
      let x = 1;
      const segs = row.values.map(function (v, s) { return { v: v, s: s }; }).filter(function (seg) { return seg.v > 0; });
      segs.forEach(function (seg, k) {
        const w = (seg.v / max) * (plotW - 1);
        const isLast = k === segs.length - 1;
        const drawW = Math.max(isLast ? w : w - 2, 1);  // 2px surface gap between stacked segments
        const g = el('g', { class: 'viz-mark' }, svg);
        el('path', { d: isLast ? barPath(x, barY, drawW, barH, 'end') : 'M' + x + ',' + barY + 'h' + drawW + 'v' + barH + 'h' + -drawW + 'Z',
                     fill: 'var(' + SERIES_VARS[seg.s] + ')' }, g);
        const hit = el('rect', { x: x, y: barY - 6, width: Math.max(w, 12), height: barH + 12, class: 'viz-hit' }, g);
        const valueText = fmt(seg.v, spec) + (spec.unit ? ' ' + spec.unit + (seg.v === 1 ? '' : 's') : '');
        const label = spec.series.length > 1 ? row.label + ' — ' + spec.series[seg.s].name : row.label;
        wireHit(hit, g, valueText, label, spec.series.length > 1 ? null : row.tip);
        x += w;
      });
      el('text', { x: x + 6, y: barY + barH - 2, class: 'viz-value' }, svg).textContent = fmt(totals[i], spec);
    });
  }

  function buildTable(spec) {
    const details = document.createElement('details');
    details.className = 'viz-table';
    const summary = document.createElement('summary'); summary.textContent = 'Show as table'; details.appendChild(summary);
    const table = document.createElement('table');
    const head = table.createTHead().insertRow();
    [''].concat(spec.series.map(function (s) { return s.name; })).forEach(function (h) {
      const th = document.createElement('th'); th.textContent = h; head.appendChild(th);
    });
    const body = table.createTBody();
    spec.rows.forEach(function (row) {
      const tr = body.insertRow();
      tr.insertCell().textContent = row.tip && spec.type === 'columns' ? row.tip : row.label;
      row.values.forEach(function (v) { const td = tr.insertCell(); td.textContent = fmt(v, spec); td.className = 'num'; });
    });
    details.appendChild(table);
    return details;
  }

  function buildLegend(spec) {
    const legend = document.createElement('div');
    legend.className = 'viz-legend';
    spec.series.forEach(function (s, i) {
      const item = document.createElement('span');
      const sw = document.createElement('span'); sw.className = 'viz-swatch'; sw.style.background = 'var(' + SERIES_VARS[i] + ')';
      item.appendChild(sw); item.appendChild(document.createTextNode(s.name));
      legend.appendChild(item);
    });
    return legend;
  }

  function mount(root) {
    const spec = JSON.parse(root.querySelector('script[type="application/json"]').textContent);
    const hasData = spec.rows.some(function (r) { return r.values.some(function (v) { return v > 0; }); });
    if (!hasData) {
      const p = document.createElement('p'); p.className = 'viz-empty';
      p.textContent = spec.empty || 'Nothing to chart yet.';
      root.appendChild(p);
      return;
    }
    if (spec.series.length > 1) root.appendChild(buildLegend(spec));
    const svg = el('svg', { class: 'viz-svg', role: 'group', 'aria-label': spec.title }, root);
    root.appendChild(buildTable(spec));
    let lastWidth = 0;
    function draw() {
      const width = Math.floor(root.clientWidth);
      if (!width || width === lastWidth) return;
      lastWidth = width;
      svg.replaceChildren();
      svg.setAttribute('width', width);
      (spec.type === 'columns' ? renderColumns : renderHbar)(svg, spec, width);
    }
    draw();
    if (window.ResizeObserver) new ResizeObserver(draw).observe(root);
  }

  document.querySelectorAll('.viz[data-chart-id]').forEach(mount);
  window.addEventListener('scroll', hideTip, true);
})();
