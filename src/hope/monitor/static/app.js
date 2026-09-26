/* Дашборд монитора hope: vanilla JS + Plotly (CDN). Только чтение JSON API.
 *
 * Структура: state -> загрузка списка запусков -> выбор запуска -> вкладки, каждая со своим load().
 * Автообновление: таймер перечитывает текущую вкладку (по умолчанию включено для live-запусков).
 * Все времена — UTC (движок пишет мс Unix).
 */
'use strict';

// ---------------------------------------------------------------- палитра (тёмная поверхность)
const T = {
  surface: '#1a1a19', ink: '#ffffff', ink2: '#c3c2b7', muted: '#898781', grid: '#2c2c2a', axis: '#383835',
  // категориальные слоты в фиксированном порядке (синий, оранжевый, аква, жёлтый, маджента, зелёный, фиолетовый, красный)
  s: ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'],
  good: '#0ca30c', warn: '#fab219', bad: '#d03b3b', neg: '#e66767',
};
const PLOT_CFG = { displayModeBar: false, responsive: true };
const FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';

function axis(over) {
  over = over || {};
  const out = Object.assign({ gridcolor: T.grid, zerolinecolor: T.axis, linecolor: T.axis, tickcolor: T.axis,
    tickfont: { color: T.muted, size: 11 } }, over);
  out.title = Object.assign({ font: { color: T.muted, size: 11 } }, over.title || {});
  return out;
}
function layout(over) {
  over = over || {};
  const base = {
    paper_bgcolor: T.surface, plot_bgcolor: T.surface,
    font: { color: T.ink2, family: FONT, size: 12 },
    margin: { l: 56, r: 16, t: 12, b: 44 },
    hovermode: 'x unified',
    hoverlabel: { bgcolor: '#262625', bordercolor: T.axis, font: { color: T.ink, size: 12 } },
    legend: { orientation: 'h', y: 1.06, x: 0, font: { color: T.ink2 } },
    showlegend: false,
  };
  const out = Object.assign({}, base, over);
  out.xaxis = axis(over.xaxis);
  out.yaxis = axis(over.yaxis);
  for (const k of Object.keys(over)) if (/^[xy]axis\d+$/.test(k)) out[k] = axis(over[k]);
  return out;
}
function plot(id, data, lay) {
  const el = document.getElementById(id);
  if (!el) return;
  if (typeof Plotly === 'undefined') {
    // CDN недоступен: таблицы и KPI работают, вместо графика — пояснение
    el.innerHTML = '<div class="empty">Plotly не загрузился (нет доступа к cdn.plot.ly) — графики недоступны, таблицы работают</div>';
    return;
  }
  Plotly.react(el, data, layout(lay), PLOT_CFG);
}
function emptyPlot(id, text) {
  plot(id, [], { xaxis: { visible: false }, yaxis: { visible: false },
    annotations: [{ text: text || 'нет данных', showarrow: false, font: { color: T.muted, size: 13 }, xref: 'paper', yref: 'paper', x: .5, y: .5 }] });
}

// ---------------------------------------------------------------- форматирование
const isNum = (x) => typeof x === 'number' && isFinite(x);
function fmt(x, d) {
  if (!isNum(x)) return '—';
  d = d == null ? 2 : d;
  return x.toLocaleString('ru-RU', { minimumFractionDigits: d, maximumFractionDigits: d });
}
function fmtSigned(x, d) { return isNum(x) ? (x > 0 ? '+' : '') + fmt(x, d) : '—'; }
function fmtPct(x, d) { return isNum(x) ? fmtSigned(x, d == null ? 2 : d) + '%' : '—'; }
function fmtShare(x, d) { return isNum(x) ? fmt(x * 100, d == null ? 1 : d) + '%' : '—'; }
function fmtInt(x) { return isNum(x) ? x.toLocaleString('ru-RU', { maximumFractionDigits: 0 }) : '—'; }
function fmtPrice(x) {
  if (!isNum(x)) return '—';
  const d = x >= 1000 ? 2 : x >= 10 ? 4 : 6;
  return x.toLocaleString('ru-RU', { minimumFractionDigits: 0, maximumFractionDigits: d });
}
function tsStr(ms, withMs) {
  if (!isNum(ms)) return '—';
  const iso = new Date(ms).toISOString();
  return withMs ? iso.replace('T', ' ').replace('Z', '') : iso.replace('T', ' ').slice(0, 19);
}
function tsPlot(ms) { return isNum(ms) ? new Date(ms).toISOString().replace('T', ' ').replace('Z', '') : null; }
function fmtDur(secs) {
  if (!isNum(secs)) return '—';
  if (secs < 60) return fmt(secs, 0) + ' с';
  if (secs < 3600) return fmt(secs / 60, 1) + ' мин';
  if (secs < 86400) return fmt(secs / 3600, 1) + ' ч';
  return fmt(secs / 86400, 1) + ' д';
}
const signCls = (x) => !isNum(x) ? '' : x > 0 ? 'pos' : x < 0 ? 'neg' : '';
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const el = (id) => document.getElementById(id);

// ---------------------------------------------------------------- таблицы
/** columns: [{key, label, fmt(v,row), num, cls(v,row)}] */
function table(container, columns, rows, opts) {
  const c = typeof container === 'string' ? el(container) : container;
  if (!rows || !rows.length) { c.innerHTML = `<div class="empty">${esc((opts && opts.empty) || 'нет данных')}</div>`; return; }
  const head = columns.map((col) => `<th class="${col.num ? 'num' : ''}">${esc(col.label)}</th>`).join('');
  const body = rows.map((r) => '<tr>' + columns.map((col) => {
    const v = r[col.key];
    const txt = col.fmt ? col.fmt(v, r) : (v == null ? '—' : esc(v));
    const cls = [col.num ? 'num' : '', col.cls ? col.cls(v, r) : ''].filter(Boolean).join(' ');
    return `<td class="${cls}">${txt}</td>`;
  }).join('') + '</tr>').join('');
  const maxH = opts && opts.maxHeight ? `style="max-height:${opts.maxHeight}px"` : '';
  c.innerHTML = `<div class="tbl-wrap" ${maxH}><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}
const colPnl = (key, label, d) => ({ key, label, num: true, fmt: (v) => fmtSigned(v, d), cls: signCls });

// ---------------------------------------------------------------- API
let toastTimer = null;
function toast(msg) {
  let t = document.querySelector('.toast');
  if (!t) { t = document.createElement('div'); t.className = 'toast'; document.body.appendChild(t); }
  t.textContent = msg;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.remove(), 6000);
}
async function api(path, params) {
  const url = new URL(path, location.origin);
  if (params) for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
  const r = await fetch(url);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (e) { /* не JSON */ }
    throw new Error(`${r.status}: ${detail}`);
  }
  return r.json();
}

// ---------------------------------------------------------------- состояние
const state = {
  runs: [], run: null, tab: 'overview', timer: null, reqToken: 0,
  fillsPage: 0, metricsNames: [], compareSelected: new Set(),
};
const runKey = (r) => String(r.id);
function currentRun() { return state.run; }

// ---------------------------------------------------------------- запуски
function runLabel(r) {
  const st = r.status === 'running' ? '● live' : r.status;
  const db = r.db_index != null && state.multiDb ? ` [${r.db_index}]` : '';
  return `#${r.run_id}${db} ${r.name || ''} · ${r.strategy} · ${r.mode} · ${st} · ${fmtSigned(r.net_pnl, 2)} USDT · ${fmtInt(r.n_fills)} исп.`;
}
async function loadRuns(keepSelection) {
  const data = await api('/api/runs');
  state.runs = data.runs;
  state.multiDb = data.multi;
  const sel = el('runSelect');
  const prev = keepSelection && state.run ? runKey(state.run) : null;
  // Перестраиваем список только если изменился набор запусков/статусов (иначе открытый select схлопывается);
  // при том же наборе просто обновляем подписи (PnL, число исполнений).
  const sig = state.runs.map((r) => runKey(r) + '|' + r.status).join(';');
  if (sel.dataset.sig !== sig) {
    sel.innerHTML = state.runs.map((r) => `<option value="${esc(runKey(r))}">${esc(runLabel(r))}</option>`).join('');
    sel.dataset.sig = sig;
  } else {
    state.runs.forEach((r, i) => { const o = sel.options[i]; if (o) o.textContent = runLabel(r); });
  }
  el('dbLabel').textContent = data.dbs.map((d) => d.path + (d.ok ? '' : ' (недоступна)')).join(', ');
  if (!state.runs.length) { toast('В БД нет запусков'); return; }
  let pick = prev && state.runs.find((r) => runKey(r) === prev);
  if (!pick) pick = state.runs.find((r) => r.status === 'running') || state.runs[0];
  sel.value = runKey(pick);
  const changed = !state.run || runKey(state.run) !== runKey(pick);
  state.run = pick;
  updateHeader();
  if (changed) onRunChanged();
}
function updateHeader() {
  const r = state.run;
  if (!r) return;
  el('liveBadge').classList.toggle('hidden', r.status !== 'running');
  const mb = el('modeBadge');
  mb.textContent = r.mode + (r.status !== 'running' ? ' · ' + r.status : '');
  mb.className = 'badge ' + (r.mode === 'backtest' ? 'backtest' : '') + (r.status === 'crashed' ? ' crashed' : '');
  mb.classList.remove('hidden');
}
function onRunChanged() {
  state.fillsPage = 0;
  // фильтры, зависящие от запуска, начинаем с чистого листа
  el('tradesSymbol').innerHTML = '<option value="">все</option>';
  el('metricName').innerHTML = '';
  el('metricSymbol').innerHTML = '<option value="">все</option>';
  el('eventLevel').innerHTML = '<option value="">все</option>';
  el('autoRefresh').checked = state.run.status === 'running';
  scheduleRefresh();
  renderTab();
}
function scheduleRefresh() {
  clearInterval(state.timer);
  state.timer = null;
  if (el('autoRefresh').checked) {
    const ms = parseInt(el('refreshMs').value, 10) || 3000;
    state.timer = setInterval(async () => {
      try { await loadRuns(true); await renderTab(); } catch (e) { toast('Ошибка обновления: ' + e.message); }
    }, ms);
  }
}

// ---------------------------------------------------------------- вкладки
async function renderTab() {
  const run = currentRun();
  if (!run) return;
  const token = ++state.reqToken;
  const main = el('main');
  main.classList.add('loading');
  try {
    await TABS[state.tab](run, token);
    el('lastUpdate').textContent = 'обновлено ' + new Date().toLocaleTimeString('ru-RU');
  } catch (e) {
    toast('Ошибка: ' + e.message);
    console.error(e);
  } finally {
    if (token === state.reqToken) main.classList.remove('loading');
  }
}
function switchTab(name) {
  state.tab = name;
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab').forEach((s) => s.classList.toggle('active', s.id === 'tab-' + name));
  renderTab().then(() => { window.dispatchEvent(new Event('resize')); });
}

// ================================================================ Обзор
function kpi(label, value, sub, cls) {
  return `<div class="kpi"><div class="label">${esc(label)}</div><div class="value ${cls || ''}">${value}</div>${sub ? `<div class="sub">${sub}</div>` : ''}</div>`;
}
async function tabOverview(run, token) {
  const [sum, eq, pos] = await Promise.all([
    api(`/api/runs/${run.id}/summary`), api(`/api/runs/${run.id}/equity`, { max_points: 2500 }), api(`/api/runs/${run.id}/positions`),
  ]);
  if (token !== state.reqToken) return;
  const s = sum.summary;
  const mk5 = s.markout_bps && s.markout_bps['5000'];
  el('kpis').innerHTML = [
    kpi('Чистый PnL', fmtSigned(s.net_pnl, 2) + ' <small>USDT</small>', `брутто ${fmtSigned(s.gross_pnl, 2)} · комиссии ${fmt(s.fees, 2)}`, signCls(s.net_pnl)),
    kpi('Доходность', fmtPct(s.return_pct), `нач. капитал ${fmt(s.initial_equity, 0)}`, signCls(s.return_pct)),
    kpi('Equity', fmt(s.equity, 2), `нереализ. ${fmtSigned(s.unrealized_pnl, 2)} · фандинг ${fmtSigned(s.funding, 2)}`),
    kpi('Просадка макс.', fmt(s.max_drawdown, 2), `${fmt(s.max_drawdown_pct, 2)}% от пика`, s.max_drawdown_pct >= 10 ? 'neg' : ''),
    kpi('Исполнений', fmtInt(s.n_fills), `${fmt(s.fills_per_day, 1)} / день · оборот ${fmt(s.turnover, 0)}`),
    kpi('Раунд-трипов', fmtInt(s.n_roundtrips), `${fmt(s.trades_per_day, 1)} / день · открытых лотов ${s.n_open_lots}`),
    kpi('Win rate', fmtShare(s.win_rate), `${s.n_wins} побед / ${s.n_losses} потерь`),
    kpi('Profit factor', fmt(s.profit_factor, 2), `ожидание/сделку ${fmtSigned(s.expectancy_per_trade, 3)}`, isNum(s.profit_factor) ? (s.profit_factor >= 1.2 ? 'pos' : s.profit_factor < 1 ? 'neg' : '') : ''),
    kpi('Доля комиссий', fmtShare(s.fee_share_of_gross, 0), 'комиссии / |брутто|', isNum(s.fee_share_of_gross) && s.fee_share_of_gross >= 0.5 ? 'neg' : ''),
    kpi('Maker', fmtShare(s.maker_share, 0), `захват спреда ${fmt(s.avg_spread_captured_bps, 2)} б.п.`),
    kpi('Sharpe', fmt(s.sharpe, 2), `Sortino ${fmt(s.sortino, 2)} · 5-мин, годовой`, isNum(s.sharpe) ? (s.sharpe >= 1 ? 'pos' : s.sharpe < 0 ? 'neg' : '') : ''),
    kpi('Markout 5 с', isNum(mk5) ? fmtSigned(mk5, 2) + ' б.п.' : '—', `1с ${fmtSigned(s.markout_bps['1000'], 2)} · 30с ${fmtSigned(s.markout_bps['30000'], 2)} · 60с ${fmtSigned(s.markout_bps['60000'], 2)}`, signCls(mk5)),
    kpi('Удержание', fmtDur(s.avg_hold_secs), `медиана ${fmtDur(s.median_hold_secs)}`),
    kpi('Лучшая / худшая', `${fmtSigned(s.best_trade, 2)} / ${fmtSigned(s.worst_trade, 2)}`, `long ${fmtSigned(s.pnl_long, 2)} · short ${fmtSigned(s.pnl_short, 2)}`),
  ].join('');

  // вердикт
  const v = s.verdict || { checks: {} };
  el('verdict').innerHTML = Object.entries(v.checks).map(([k, c]) => {
    const val = k === 'enough_trades' ? fmtInt(c.value) : k === 'drawdown_pct' ? fmt(c.value, 2) + '%' : k === 'fee_share' ? fmtShare(c.value, 0) : fmt(c.value, 2);
    return `<li class="${c.ok ? 'ok' : 'fail'}"><span class="mark">${c.ok ? '✓' : '✗'}</span><span class="lbl">${esc(c.label)}</span><span class="val">${val}</span><span class="thr">${esc(c.threshold)}</span></li>`;
  }).join('');
  el('verdictTotal').textContent = `${v.n_ok} из ${v.n_total} проверок пройдено` + (v.all_ok ? ' — всё зелёное' : '');

  // информация о запуске
  const r = sum.run;
  el('runInfo').innerHTML = [
    ['Имя', r.name], ['Стратегия', r.strategy], ['Движок', `${r.engine} · ${r.branch || '—'}`], ['Режим', `${r.mode} · ${r.status}`],
    ['Старт', tsStr(r.started_ts)], ['Финиш', r.finished_ts ? tsStr(r.finished_ts) : '—'], ['Длительность', fmtDur(s.duration_secs)],
    ['Символы', (r.symbols || []).join(', ')], ['БД', r.db],
  ].map(([k, val]) => `<dt>${esc(k)}</dt><dd>${esc(val)}</dd>`).join('');
  el('runParams').textContent = JSON.stringify(r.params || {}, null, 1);

  // equity + просадка: две панели с общей осью X
  if (eq.n) {
    const x = eq.ts.map(tsPlot);
    plot('equityChart', [
      { x, y: eq.equity, type: 'scatter', mode: 'lines', name: 'Equity', line: { color: T.s[0], width: 2 }, hovertemplate: '%{y:.2f}<extra>equity</extra>' },
      { x, y: eq.peak, type: 'scatter', mode: 'lines', name: 'Пик', line: { color: T.muted, width: 1 }, hoverinfo: 'skip' },
      { x, y: eq.drawdown_pct.map((d) => -d), type: 'scatter', mode: 'lines', name: 'Просадка, %', yaxis: 'y2', fill: 'tozeroy',
        line: { color: T.neg, width: 1.5 }, fillcolor: 'rgba(230,103,103,0.18)', hovertemplate: '%{y:.2f}%<extra>просадка</extra>' },
    ], {
      showlegend: true, margin: { l: 64, r: 16, t: 28, b: 44 },
      xaxis: { anchor: 'y2' },  // подписи оси X внизу, под панелью просадки, а не между панелями
      yaxis: { domain: [0.34, 1], title: { text: 'USDT' } },
      yaxis2: { domain: [0, 0.26], title: { text: '%' } },
    });
  } else emptyPlot('equityChart', 'снимков equity ещё нет');

  table('positionsTable', [
    { key: 'symbol', label: 'Символ' }, { key: 'side', label: 'Сторона' },
    { key: 'qty', label: 'Кол-во', num: true, fmt: (v) => fmt(v, 4) },
    { key: 'avg_price', label: 'Средняя', num: true, fmt: fmtPrice }, { key: 'mark', label: 'Марк', num: true, fmt: fmtPrice },
    { key: 'notional', label: 'Номинал', num: true, fmt: (v) => fmt(v, 2) },
    colPnl('unrealized_pnl', 'Нереализ.'), colPnl('realized_pnl', 'Реализ.'),
    { key: 'fees', label: 'Комиссии', num: true, fmt: (v) => fmt(v, 4) }, colPnl('funding', 'Фандинг', 4), colPnl('net_pnl', 'Итого'),
    { key: 'n_fills', label: 'Исп.', num: true, fmt: fmtInt },
    { key: 'opened_ts', label: 'Открыта', fmt: (v) => v ? tsStr(v) : '—' }, { key: 'updated_ts', label: 'Обновлена', fmt: (v) => tsStr(v) },
  ], pos.rows, { empty: 'позиций нет', maxHeight: 240 });
}

// ================================================================ Символы
async function tabSymbols(run, token) {
  const data = await api(`/api/runs/${run.id}/symbols`);
  if (token !== state.reqToken) return;
  const rows = data.rows;
  if (rows.length) {
    const x = rows.map((r) => r.symbol);
    const y = rows.map((r) => r.rt_net_pnl);
    plot('symbolsChart', [{ x, y, type: 'bar', marker: { color: y.map((v) => v >= 0 ? T.good : T.neg) }, hovertemplate: '%{y:+.2f} USDT<extra></extra>' }],
      { yaxis: { title: { text: 'USDT' }, zeroline: true }, bargap: 0.4 });
    const st = rows.filter((r) => r.stats);
    if (st.length) {
      plot('symbolsMarketChart', [
        { x: st.map((r) => r.symbol), y: st.map((r) => r.stats.spread_bps), name: 'Спред, б.п.', type: 'bar', marker: { color: T.s[0] } },
        { x: st.map((r) => r.symbol), y: st.map((r) => r.stats.vol_bps), name: 'Волатильность, б.п.', type: 'bar', marker: { color: T.s[1] } },
      ], { showlegend: true, barmode: 'group', bargap: 0.3, yaxis: { title: { text: 'б.п.' } } });
    } else emptyPlot('symbolsMarketChart', 'статистики по символам нет');
  } else { emptyPlot('symbolsChart'); emptyPlot('symbolsMarketChart'); }
  const st = (key, d) => ({ key: 'stats', label: { spread_bps: 'Спред б.п.', spread_med_bps: 'Спред мед.', vol_bps: 'Вол. б.п.', trades_per_min: 'Сделок/мин', turnover_per_min: 'Оборот/мин', flow_imbalance: 'Дисбаланс', mid: 'Mid' }[key], num: true, fmt: (v) => v ? (key === 'mid' ? fmtPrice(v[key]) : fmt(v[key], d)) : '—' });
  table('symbolsTable', [
    { key: 'symbol', label: 'Символ' },
    { key: 'n_fills', label: 'Исп.', num: true, fmt: fmtInt }, { key: 'n_roundtrips', label: 'Раунд-трипов', num: true, fmt: fmtInt },
    colPnl('realized_pnl', 'Реализ.'), { key: 'fees', label: 'Комиссии', num: true, fmt: (v) => fmt(v, 2) }, colPnl('net_pnl', 'Чистый'),
    colPnl('rt_net_pnl', 'Чистый (РТ)'), { key: 'win_rate', label: 'Win rate', num: true, fmt: (v) => fmtShare(v) },
    { key: 'maker_share', label: 'Maker', num: true, fmt: (v) => fmtShare(v, 0) }, { key: 'avg_hold_secs', label: 'Удерж.', num: true, fmt: fmtDur },
    { key: 'position_qty', label: 'Позиция', num: true, fmt: (v) => fmt(v, 4), cls: signCls }, colPnl('unrealized_pnl', 'Нереализ.'),
    st('mid'), st('spread_bps', 2), st('spread_med_bps', 2), st('vol_bps', 1), st('trades_per_min', 0), st('turnover_per_min', 0), st('flow_imbalance', 2),
    { key: 'stats', label: 'Статистика на', fmt: (v) => v ? tsStr(v.ts) : '—' },
  ], rows);
}

// ================================================================ Сделки
async function tabTrades(run, token) {
  const symbol = el('tradesSymbol').value;
  const pageSize = parseInt(el('fillsPageSize').value, 10);
  const [rt, fills, hours, sum] = await Promise.all([
    api(`/api/runs/${run.id}/roundtrips`, { limit: el('rtLimit').value, symbol }),
    api(`/api/runs/${run.id}/fills`, { limit: pageSize, offset: state.fillsPage * pageSize, symbol }),
    api(`/api/runs/${run.id}/pnl_by_hour`),
    api(`/api/runs/${run.id}/summary`),
  ]);
  if (token !== state.reqToken) return;
  // список символов для фильтра
  const symSel = el('tradesSymbol');
  const have = new Set(Array.from(symSel.options).map((o) => o.value));
  for (const s of fills.symbols || []) if (!have.has(s)) symSel.insertAdjacentHTML('beforeend', `<option value="${esc(s)}">${esc(s)}</option>`);

  if (rt.cum.close_ts && rt.cum.close_ts.length) {
    plot('cumPnlChart', [{ x: rt.cum.close_ts.map(tsPlot), y: rt.cum.cum_net_pnl, type: 'scatter', mode: 'lines', line: { color: T.s[0], width: 2 }, fill: 'tozeroy', fillcolor: 'rgba(57,135,229,0.12)', hovertemplate: '%{y:+.2f}<extra></extra>' }],
      { yaxis: { title: { text: 'USDT' }, zeroline: true } });
    const e = rt.hist.edges, c = rt.hist.counts;
    const centers = c.map((_, i) => (e[i] + e[i + 1]) / 2);
    plot('pnlHistChart', [{ x: centers, y: c, type: 'bar', width: centers.map((_, i) => (e[i + 1] - e[i]) * 0.92), marker: { color: centers.map((v) => v >= 0 ? T.good : T.neg) }, hovertemplate: '%{x:.2f}: %{y}<extra></extra>' }],
      { xaxis: { title: { text: 'чистый PnL раунд-трипа, USDT' } }, yaxis: { title: { text: 'штук' } }, hovermode: 'closest' });
  } else { emptyPlot('cumPnlChart', 'закрытых раунд-трипов нет'); emptyPlot('pnlHistChart', 'закрытых раунд-трипов нет'); }

  const hr = hours.rows || [];
  if (hr.some((h) => h.n > 0)) {
    plot('pnlHourChart', [{ x: hr.map((h) => h.hour), y: hr.map((h) => h.net_pnl), type: 'bar', marker: { color: hr.map((h) => h.net_pnl >= 0 ? T.good : T.neg) },
      customdata: hr.map((h) => [h.n, h.win_rate == null ? null : h.win_rate * 100]), hovertemplate: '%{x}:00 UTC · %{y:+.2f} USDT · сделок %{customdata[0]} · win %{customdata[1]:.0f}%<extra></extra>' }],
      { xaxis: { title: { text: 'час UTC' }, dtick: 2 }, yaxis: { title: { text: 'USDT' }, zeroline: true }, hovermode: 'closest' });
  } else emptyPlot('pnlHourChart');
  const tags = Object.entries(sum.summary.pnl_by_tag || {}).sort((a, b) => b[1] - a[1]);
  if (tags.length) {
    plot('pnlTagChart', [{ x: tags.map((t) => t[0] || '(без тега)'), y: tags.map((t) => t[1]), type: 'bar', marker: { color: tags.map((t) => t[1] >= 0 ? T.good : T.neg) }, hovertemplate: '%{y:+.2f} USDT<extra></extra>' }],
      { yaxis: { title: { text: 'USDT' }, zeroline: true }, bargap: 0.4, hovermode: 'closest' });
  } else emptyPlot('pnlTagChart');

  el('rtCount').textContent = `(закрытых ${fmtInt(rt.total)}, показано ${rt.rows.length}, открытых лотов ${rt.open.length})`;
  const rtRows = rt.open.map((r) => Object.assign({}, r, { _open: true })).concat(rt.rows);
  table('rtTable', [
    { key: 'symbol', label: 'Символ' }, { key: 'side', label: 'Сторона', cls: (v) => v === 'long' ? 'pos' : 'neg' },
    { key: 'open_ts', label: 'Открыт', fmt: (v) => tsStr(v) }, { key: 'close_ts', label: 'Закрыт', fmt: (v, r) => r.open ? '<span class="muted">открыт</span>' : tsStr(v) },
    { key: 'hold_secs', label: 'Удерж.', num: true, fmt: fmtDur }, { key: 'qty', label: 'Кол-во', num: true, fmt: (v) => fmt(v, 4) },
    { key: 'entry_price', label: 'Вход', num: true, fmt: fmtPrice }, { key: 'exit_price', label: 'Выход', num: true, fmt: fmtPrice },
    colPnl('gross_pnl', 'Брутто', 3), { key: 'fees', label: 'Комиссии', num: true, fmt: (v) => fmt(v, 4) }, colPnl('net_pnl', 'Чистый', 3),
    { key: 'n_fills', label: 'Исп.', num: true }, { key: 'entry_tag', label: 'Тег входа' }, { key: 'exit_tag', label: 'Тег выхода' },
    { key: 'maker_share', label: 'Maker', num: true, fmt: (v) => fmtShare(v, 0) }, { key: 'entry_seq', label: 'seq', num: true, fmt: (v, r) => `${v}→${r.exit_seq == null ? '…' : r.exit_seq}` },
  ], rtRows, { empty: 'раунд-трипов ещё нет' });

  const pages = Math.max(1, Math.ceil(fills.total / pageSize));
  if (state.fillsPage >= pages) state.fillsPage = pages - 1;
  el('fillsCount').textContent = `(всего ${fmtInt(fills.total)})`;
  el('fillsPage').textContent = `стр. ${state.fillsPage + 1} / ${pages}`;
  el('fillsPrev').disabled = state.fillsPage <= 0;
  el('fillsNext').disabled = state.fillsPage >= pages - 1;
  table('fillsTable', [
    { key: 'seq', label: 'seq', num: true }, { key: 'ts', label: 'Время', fmt: (v) => tsStr(v, true) }, { key: 'symbol', label: 'Символ' },
    { key: 'side', label: 'Сторона', cls: (v) => v === 'Buy' ? 'pos' : 'neg' }, { key: 'price', label: 'Цена', num: true, fmt: fmtPrice },
    { key: 'qty', label: 'Кол-во', num: true, fmt: (v) => fmt(v, 4) }, { key: 'notional', label: 'Номинал', num: true, fmt: (v) => fmt(v, 2) },
    { key: 'fee', label: 'Комиссия', num: true, fmt: (v) => fmt(v, 5) }, { key: 'is_maker', label: 'Maker', fmt: (v) => v ? 'maker' : 'taker' },
    { key: 'purpose', label: 'Назначение' }, { key: 'tag', label: 'Тег' }, colPnl('realized_pnl', 'Реализ.', 4),
    { key: 'position_after', label: 'Позиция после', num: true, fmt: (v) => fmt(v, 4), cls: signCls },
    { key: 'bid', label: 'Bid', num: true, fmt: fmtPrice }, { key: 'ask', label: 'Ask', num: true, fmt: fmtPrice },
    { key: 'spread_captured_bps', label: 'Захват б.п.', num: true, fmt: (v) => fmtSigned(v, 2), cls: signCls },
    { key: 'spread_bps_at_place', label: 'Спред при выст.', num: true, fmt: (v) => fmt(v, 2) },
    { key: 'queue_ahead_initial', label: 'Очередь', num: true, fmt: (v) => fmt(v, 1) }, { key: 'order_id', label: 'Ордер', num: true },
  ], fills.rows, { empty: 'исполнений ещё нет' });
}

// ================================================================ Ордера
async function tabOrders(run, token) {
  const d = await api(`/api/runs/${run.id}/orders`, { limit: 300 });
  if (token !== state.reqToken) return;
  const lo = d.limit_orders;
  const sc = d.status_counts || {};
  el('ordersKpis').innerHTML = [
    kpi('Всего ордеров', fmtInt(d.total), `лимитных ${lo ? fmtInt(lo.n) : 0} · тейкер ${fmtInt(d.taker_orders.n)}`),
    kpi('Исполнено', fmtInt(sc.filled || 0), `частично ${fmtInt(sc.partial_cancelled || 0)}`),
    kpi('Отменено', fmtInt(sc.cancelled || 0), `post-only отказ ${fmtInt(sc.rejected_post_only || 0)}`),
    kpi('Fill ratio лимитных', lo ? fmtShare(lo.fill_ratio_count, 0) : '—', lo ? `по объёму ${fmtShare(lo.fill_ratio_volume, 0)}` : 'лимитных ордеров нет'),
    kpi('Время до финала', lo ? fmtDur(lo.avg_time_to_done_ms / 1000) : '—', lo ? `медиана ${fmtDur(lo.median_time_to_done_ms / 1000)}` : ''),
    kpi('Очередь впереди', lo ? fmt(lo.avg_queue_ahead, 1) : '—', lo ? `спред при выст. ${fmt(lo.avg_spread_bps_at_place, 2)} б.п.` : ''),
  ].join('');
  if (d.total) {
    const purposes = [...new Set(d.by_status_purpose.map((r) => r.purpose))];
    const statuses = ['filled', 'partial_cancelled', 'cancelled', 'rejected_post_only'];
    const extra = [...new Set(d.by_status_purpose.map((r) => r.status))].filter((s) => !statuses.includes(s));
    const all = statuses.concat(extra);
    plot('ordersStatusChart', all.map((st, i) => ({
      x: purposes, y: purposes.map((p) => (d.by_status_purpose.find((r) => r.purpose === p && r.status === st) || {}).n || 0),
      name: st, type: 'bar', marker: { color: T.s[i % 8] },
    })), { barmode: 'stack', showlegend: true, bargap: 0.4, yaxis: { title: { text: 'ордеров' } } });
    if (d.queue_bins.length) {
      const qb = d.queue_bins;
      plot('ordersQueueChart', [{
        x: qb.map((b) => `${fmt(Math.max(0, b.lo), 0)}–${fmt(b.hi, 0)}`), y: qb.map((b) => b.fill_ratio_count * 100), type: 'bar', marker: { color: T.s[0] },
        text: qb.map((b) => isNum(b.filled_share) ? fmt(b.filled_share * 100, 0) + '%' : ''), textposition: 'outside', textfont: { color: T.ink2 },
        customdata: qb.map((b) => [b.n, b.avg_time_to_done_ms / 1000]), hovertemplate: 'очередь %{x}<br>исполнено полностью %{y:.0f}%<br>ордеров %{customdata[0]}<br>время до финала %{customdata[1]:.1f} с<extra></extra>',
      }], { xaxis: { title: { text: 'queue_ahead_initial' } }, yaxis: { title: { text: '% исполненных' }, range: [0, 110] }, hovermode: 'closest', bargap: 0.3 });
    } else emptyPlot('ordersQueueChart', 'мало лимитных ордеров для корзин');
  } else { emptyPlot('ordersStatusChart', 'ордеров ещё нет'); emptyPlot('ordersQueueChart', 'ордеров ещё нет'); }
  table('ordersTable', [
    { key: 'order_id', label: 'ID', num: true }, { key: 'ts_created', label: 'Создан', fmt: (v) => tsStr(v, true) },
    { key: 'ts_done', label: 'Завершён', fmt: (v) => tsStr(v, true) }, { key: 'time_to_done_ms', label: 'Длит.', num: true, fmt: (v) => fmtDur(v / 1000) },
    { key: 'symbol', label: 'Символ' }, { key: 'side', label: 'Сторона', cls: (v) => v === 'Buy' ? 'pos' : 'neg' },
    { key: 'price', label: 'Цена', num: true, fmt: (v) => v ? fmtPrice(v) : 'market' }, { key: 'qty', label: 'Кол-во', num: true, fmt: (v) => fmt(v, 4) },
    { key: 'filled', label: 'Исполнено', num: true, fmt: (v) => fmt(v, 4) }, { key: 'status', label: 'Статус', cls: (v) => v === 'filled' ? 'pos' : v === 'rejected_post_only' ? 'neg' : '' },
    { key: 'taker', label: 'Тип', fmt: (v) => v ? 'taker' : 'limit' }, { key: 'purpose', label: 'Назначение' }, { key: 'tag', label: 'Тег' },
    { key: 'queue_ahead_initial', label: 'Очередь', num: true, fmt: (v) => fmt(v, 1) }, { key: 'spread_bps_at_place', label: 'Спред б.п.', num: true, fmt: (v) => fmt(v, 2) },
  ], d.rows, { empty: 'ордеров ещё нет' });
}

// ================================================================ Markout
function markoutBars(id, rows, groupKey, labelOf) {
  if (!rows || !rows.length) { emptyPlot(id, 'markout ещё не посчитан'); return; }
  const horizons = [...new Set(rows.map((r) => r.horizon_ms))].sort((a, b) => a - b);
  // groupKey=null — одна серия (все исполнения), бары окрашены по знаку
  const groups = groupKey ? [...new Set(rows.map((r) => String(r[groupKey])))] : ['all'];
  if (!groupKey) { groupKey = '_all'; rows = rows.map((r) => Object.assign({ _all: 'all' }, r)); }
  const hLabel = (h) => (h >= 1000 ? h / 1000 + ' с' : h + ' мс');
  const traces = groups.slice(0, 8).map((g, i) => ({
    x: horizons.map(hLabel),
    y: horizons.map((h) => { const r = rows.find((q) => String(q[groupKey]) === g && q.horizon_ms === h); return r ? r.avg : null; }),
    customdata: horizons.map((h) => { const r = rows.find((q) => String(q[groupKey]) === g && q.horizon_ms === h); return r ? [r.n, r.median] : [0, null]; }),
    name: labelOf ? labelOf(g) : g, type: 'bar', marker: { color: groups.length === 1 ? undefined : T.s[i] },
    hovertemplate: '%{y:+.2f} б.п. (медиана %{customdata[1]:+.2f}, n=%{customdata[0]})<extra>%{fullData.name}</extra>',
  }));
  if (groups.length === 1) traces[0].marker.color = traces[0].y.map((v) => v >= 0 ? T.good : T.neg);
  plot(id, traces, { barmode: 'group', showlegend: groups.length > 1, yaxis: { title: { text: 'б.п.' }, zeroline: true }, bargap: 0.3, hovermode: 'closest' });
}
async function tabMarkout(run, token) {
  const d = await api(`/api/runs/${run.id}/markouts`);
  if (token !== state.reqToken) return;
  el('markoutN').textContent = d.n ? `(исполнений с markout: ${fmtInt(d.n)})` : '';
  markoutBars('markoutChart', d.by_horizon, null, () => 'все исполнения');
  markoutBars('markoutMakerChart', d.by_maker, 'is_maker', (g) => g === '1' ? 'maker' : g === '0' ? 'taker' : 'неизвестно');
  markoutBars('markoutPurposeChart', d.by_purpose, 'purpose');
  const sideSym = (d.by_side || []).map((r) => Object.assign({}, r, { grp: 'сторона ' + r.side })).concat((d.by_symbol || []).map((r) => Object.assign({}, r, { grp: r.symbol })));
  markoutBars('markoutSideChart', sideSym, 'grp');
  const rows = [].concat(
    (d.by_horizon || []).map((r) => Object.assign({ group: 'все' }, r)),
    (d.by_maker || []).map((r) => Object.assign({ group: r.is_maker === 1 ? 'maker' : r.is_maker === 0 ? 'taker' : '?' }, r)),
    (d.by_purpose || []).map((r) => Object.assign({ group: 'purpose=' + r.purpose }, r)),
    (d.by_side || []).map((r) => Object.assign({ group: 'side=' + r.side }, r)),
    (d.by_symbol || []).map((r) => Object.assign({ group: r.symbol }, r)),
  );
  table('markoutTable', [
    { key: 'group', label: 'Группа' }, { key: 'horizon_ms', label: 'Горизонт, мс', num: true }, { key: 'n', label: 'n', num: true },
    { key: 'avg', label: 'Среднее б.п.', num: true, fmt: (v) => fmtSigned(v, 3), cls: signCls }, { key: 'median', label: 'Медиана б.п.', num: true, fmt: (v) => fmtSigned(v, 3), cls: signCls },
    { key: 'std', label: 'Ст. откл.', num: true, fmt: (v) => fmt(v, 3) },
  ], rows, { empty: 'markout ещё не посчитан', maxHeight: 360 });
}

// ================================================================ Метрики
async function tabMetrics(run, token) {
  const list = await api(`/api/runs/${run.id}/metrics`);
  if (token !== state.reqToken) return;
  const names = [...new Set((list.names || []).map((r) => r.name))];
  const nameSel = el('metricName'), symSel = el('metricSymbol');
  const prevName = nameSel.value, prevSym = symSel.value;
  nameSel.innerHTML = names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
  if (names.includes(prevName)) nameSel.value = prevName;
  const name = nameSel.value;
  const syms = [...new Set((list.names || []).filter((r) => r.name === name).map((r) => r.symbol))];
  symSel.innerHTML = '<option value="">все</option>' + syms.map((s) => `<option value="${esc(s)}">${esc(s || '(без символа)')}</option>`).join('');
  if (syms.includes(prevSym)) symSel.value = prevSym;
  table('metricsList', [
    { key: 'name', label: 'Метрика' }, { key: 'symbol', label: 'Символ' }, { key: 'n', label: 'Точек', num: true, fmt: fmtInt },
    { key: 'first_ts', label: 'Первая', fmt: (v) => tsStr(v) }, { key: 'last_ts', label: 'Последняя', fmt: (v) => tsStr(v) },
  ], list.names, { empty: 'стратегия не пишет метрик', maxHeight: 240 });
  if (!name) { emptyPlot('metricChart', 'метрик нет'); el('metricTitle').textContent = 'Метрика'; return; }
  const d = await api(`/api/runs/${run.id}/metrics`, { name, symbol: symSel.value || undefined, max_points: el('metricPoints').value });
  if (token !== state.reqToken) return;
  el('metricTitle').textContent = `${name}${symSel.value ? ' · ' + symSel.value : ''}`;
  if (!d.series.length) { emptyPlot('metricChart'); return; }
  plot('metricChart', d.series.slice(0, 8).map((s, i) => ({
    x: s.ts.map(tsPlot), y: s.value, type: 'scatter', mode: 'lines', name: s.symbol || name, line: { color: T.s[i], width: 1.5 },
  })), { showlegend: d.series.length > 1, yaxis: { title: { text: name } } });
}

// ================================================================ События
async function tabEvents(run, token) {
  const d = await api(`/api/runs/${run.id}/events`, { limit: el('eventLimit').value, level: el('eventLevel').value || undefined });
  if (token !== state.reqToken) return;
  const sel = el('eventLevel');
  const prev = sel.value;
  sel.innerHTML = '<option value="">все</option>' + (d.levels || []).map((l) => `<option value="${esc(l)}">${esc(l)}</option>`).join('');
  sel.value = prev;
  el('eventsCount').textContent = `(всего ${fmtInt(d.total)})`;
  table('eventsTable', [
    { key: 'ts', label: 'Время', fmt: (v) => tsStr(v, true) },
    { key: 'level', label: 'Уровень', cls: (v) => v === 'error' || v === 'critical' ? 'neg' : v === 'warning' || v === 'warn' ? '' : '' },
    { key: 'symbol', label: 'Символ' }, { key: 'msg', label: 'Сообщение', fmt: (v) => `<span style="white-space:normal">${esc(v)}</span>` },
  ], d.rows, { empty: 'событий нет', maxHeight: 640 });
}

// ================================================================ Сравнение
function renderCompareChecks() {
  const c = el('compareChecks');
  if (!state.compareSelected.size && state.run) state.compareSelected.add(runKey(state.run));
  c.innerHTML = state.runs.map((r) => `<label><input type="checkbox" value="${esc(runKey(r))}" ${state.compareSelected.has(runKey(r)) ? 'checked' : ''}> #${r.run_id}${state.multiDb ? ' [' + r.db_index + ']' : ''} ${esc(r.name || '')} · ${esc(r.mode)}</label>`).join('');
  c.querySelectorAll('input').forEach((i) => i.addEventListener('change', () => { if (i.checked) state.compareSelected.add(i.value); else state.compareSelected.delete(i.value); }));
}
async function tabCompare(run, token) {
  renderCompareChecks();
  const ids = [...state.compareSelected];
  if (!ids.length) { emptyPlot('compareChart', 'выберите запуски'); el('compareTable').innerHTML = ''; return; }
  const d = await api('/api/compare', { run_ids: ids.join(','), max_points: 1500 });
  if (token !== state.reqToken) return;
  if (d.errors && d.errors.length) toast('Не найдены запуски: ' + d.errors.join(', '));
  const byTs = el('compareX').value === 'ts';
  plot('compareChart', d.runs.slice(0, 8).map((r, i) => ({
    x: byTs ? r.curve.ts.map(tsPlot) : r.curve.t_secs.map((s) => s / 3600), y: r.curve.ret_pct, type: 'scatter', mode: 'lines',
    name: `#${r.run_id} ${r.name || ''} (${r.mode})`, line: { color: T.s[i], width: 2 }, hovertemplate: '%{y:+.3f}%<extra>%{fullData.name}</extra>',
  })), { showlegend: true, xaxis: { title: { text: byTs ? 'UTC' : 'часов от старта' } }, yaxis: { title: { text: '% от начального капитала' }, zeroline: true }, margin: { l: 64, r: 16, t: 32, b: 44 } });
  const S = (k, label, f, cls) => ({ key: 'summary', label, num: true, fmt: (v) => f(v ? v[k] : null), cls: cls ? (v) => cls(v ? v[k] : null) : undefined });
  table('compareTable', [
    { key: 'run_id', label: '#', num: true }, { key: 'name', label: 'Имя' }, { key: 'strategy', label: 'Стратегия' }, { key: 'mode', label: 'Режим' }, { key: 'status', label: 'Статус' },
    S('duration_secs', 'Длит.', fmtDur), S('net_pnl', 'Чистый PnL', (v) => fmtSigned(v, 2), signCls), S('return_pct', 'Дох. %', fmtPct, signCls),
    S('fees', 'Комиссии', (v) => fmt(v, 2)), S('fee_share_of_gross', 'Доля ком.', (v) => fmtShare(v, 0)), S('n_fills', 'Исп.', fmtInt), S('n_roundtrips', 'РТ', fmtInt),
    S('trades_per_day', 'РТ/день', (v) => fmt(v, 1)), S('win_rate', 'Win', (v) => fmtShare(v)), S('profit_factor', 'PF', (v) => fmt(v, 2)), S('expectancy_per_trade', 'Ожид./сделку', (v) => fmtSigned(v, 3), signCls),
    S('avg_hold_secs', 'Удерж.', fmtDur), S('maker_share', 'Maker', (v) => fmtShare(v, 0)), S('max_drawdown_pct', 'DD %', (v) => fmt(v, 2)), S('sharpe', 'Sharpe', (v) => fmt(v, 2), signCls),
    S('sortino', 'Sortino', (v) => fmt(v, 2)), S('markout_5s_bps', 'Markout 5с', (v) => fmtSigned(v, 2), signCls),
    { key: 'verdict', label: 'Вердикт', fmt: (v) => v ? `${v.n_ok}/${v.n_total}` : '—', cls: (v) => v && v.all_ok ? 'pos' : '' },
  ], d.runs, { empty: 'нет данных' });
}

const TABS = { overview: tabOverview, symbols: tabSymbols, trades: tabTrades, orders: tabOrders, markout: tabMarkout, metrics: tabMetrics, events: tabEvents, compare: tabCompare };

// ---------------------------------------------------------------- события UI
function bind() {
  el('runSelect').addEventListener('change', (e) => {
    state.run = state.runs.find((r) => runKey(r) === e.target.value) || null;
    updateHeader();
    onRunChanged();
  });
  el('autoRefresh').addEventListener('change', scheduleRefresh);
  el('refreshMs').addEventListener('change', scheduleRefresh);
  el('refreshNow').addEventListener('click', async () => { await loadRuns(true); renderTab(); });
  document.querySelectorAll('#tabs button').forEach((b) => b.addEventListener('click', () => switchTab(b.dataset.tab)));
  const rerender = () => renderTab();
  ['tradesSymbol', 'rtLimit', 'metricName', 'metricSymbol', 'metricPoints', 'eventLevel', 'eventLimit', 'compareX'].forEach((id) => el(id).addEventListener('change', rerender));
  el('fillsPageSize').addEventListener('change', () => { state.fillsPage = 0; renderTab(); });
  el('fillsPrev').addEventListener('click', () => { state.fillsPage = Math.max(0, state.fillsPage - 1); renderTab(); });
  el('fillsNext').addEventListener('click', () => { state.fillsPage += 1; renderTab(); });
  el('compareGo').addEventListener('click', rerender);
  document.addEventListener('visibilitychange', () => { if (document.hidden) { clearInterval(state.timer); state.timer = null; } else scheduleRefresh(); });
}

(async function init() {
  bind();
  try { await loadRuns(false); } catch (e) { toast('Не удалось загрузить список запусков: ' + e.message); }
})();
