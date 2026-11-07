const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const brl = new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' });
const percent = new Intl.NumberFormat('pt-BR', { style: 'percent', minimumFractionDigits: 2, maximumFractionDigits: 4 });
const unitsFormat = new Intl.NumberFormat('pt-BR', { maximumFractionDigits: 6 });
let dashboardData = null;
let currentUser = null;
let activeBrokerage = null;
let agoraPreview = null;
let contributionData = null;
let toastTimer = null;
const fxRateCache = new Map();
const fxRateRequests = new Map();

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[char]);
}

function money(value) { return value == null ? '—' : brl.format(value); }
function pct(value) { return value == null ? '—' : percent.format(value); }
function projectionCell(value, elapsed, minimum, formatter) {
  if (value != null) return formatter(value);
  if ((elapsed || 0) < minimum) {
    return `<span title="Projeção disponível após ${minimum} dias de histórico">—</span>`;
  }
  return '—';
}
function displayDate(value) {
  if (!value) return '—';
  const [year, month, day] = value.split('-').map(Number);
  return new Date(year, month - 1, day).toLocaleDateString('pt-BR');
}

function sortableCellValue(cell) {
  const input = cell.querySelector('input, select');
  const text = (input ? input.value : cell.textContent).trim();
  if (!text || text === '—') return null;
  if (cell.classList.contains('date-cell')) {
    const match = text.match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
    if (match) return Date.UTC(Number(match[3]), Number(match[2]) - 1, Number(match[1]));
  }
  if (cell.classList.contains('numeric')) {
    const normalized = input
      ? text
      : text.replace(/[R$%\s]/g, '').replace(/\./g, '').replace(',', '.');
    const number = Number(normalized);
    if (Number.isFinite(number)) return number;
  }
  return text.toLocaleLowerCase('pt-BR');
}

function initializeSortableTables(root = document) {
  const tables = root.matches?.('table') ? [root] : [...root.querySelectorAll('table')];
  for (const table of tables) {
    if (table.dataset.sortableReady === '1') continue;
    table.dataset.sortableReady = '1';
    const header = table.tHead?.rows[0];
    if (!header) continue;
    [...header.cells].forEach((cell) => {
      const label = cell.textContent.trim();
      if (!label) return;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'table-sort-button';
      button.title = `Ordenar por ${label}`;
      button.setAttribute('aria-label', `Ordenar por ${label}`);
      const text = document.createElement('span');
      text.textContent = label;
      const indicator = document.createElement('span');
      indicator.className = 'sort-indicator';
      indicator.setAttribute('aria-hidden', 'true');
      indicator.textContent = '↕';
      button.append(text, indicator);
      cell.replaceChildren(button);
      cell.setAttribute('aria-sort', 'none');
      button.addEventListener('click', () => {
        const ascending = cell.dataset.sortDirection !== 'ascending';
        for (const other of header.cells) {
          other.dataset.sortDirection = '';
          other.setAttribute('aria-sort', 'none');
          const icon = other.querySelector('.sort-indicator');
          if (icon) icon.textContent = '↕';
        }
        cell.dataset.sortDirection = ascending ? 'ascending' : 'descending';
        cell.setAttribute('aria-sort', cell.dataset.sortDirection);
        indicator.textContent = ascending ? '↑' : '↓';
        const tbody = table.tBodies[0];
        if (!tbody) return;
        const columnIndex = [...header.cells].indexOf(cell);
        const rows = [...tbody.rows].filter((row) => row.cells[columnIndex] && !row.cells[columnIndex].hasAttribute('colspan'));
        rows.sort((left, right) => {
          const a = sortableCellValue(left.cells[columnIndex]);
          const b = sortableCellValue(right.cells[columnIndex]);
          if (a == null) return b == null ? 0 : 1;
          if (b == null) return -1;
          const order = typeof a === 'number' && typeof b === 'number'
            ? a - b
            : String(a).localeCompare(String(b), 'pt-BR', { numeric: true, sensitivity: 'base' });
          return ascending ? order : -order;
        });
        tbody.append(...rows);
      });
    });
  }
}

async function api(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const response = await fetch(url, { ...options, headers, credentials: 'same-origin' });
  if (response.status === 401) {
    window.location.assign('/login');
    throw new Error('Sessão expirada.');
  }
  if (response.status === 403 && response.headers.get('X-Password-Change-Required') === '1') {
    openChangePassword(true);
  }
  const body = response.status === 204 ? null : await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body?.detail;
    throw new Error(Array.isArray(detail) ? detail.map((item) => item.msg).join(' · ') : (detail || 'Não foi possível concluir a operação.'));
  }
  return body;
}

function notify(message, isError = false) {
  const toast = $('#toast');
  toast.textContent = message;
  toast.classList.toggle('error', isError);
  toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 3200);
}

function fxAmount(value) {
  return `R$ ${new Intl.NumberFormat('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value)}`;
}

function fxRateAmount(currency, value) {
  const decimals = currency === 'USD' ? 4 : 2;
  return `R$ ${new Intl.NumberFormat('pt-BR', { minimumFractionDigits: decimals, maximumFractionDigits: decimals }).format(value)}`;
}

function displayFxTime(value) {
  if (!value) return '';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString('pt-BR');
}

function fxQuoteDescription(quote) {
  const when = displayFxTime(quote.quote_time);
  return `1 ${quote.currency} = ${fxRateAmount(quote.currency, quote.brl_per_unit)} · ${quote.source}${when ? ` · cotação de ${when}` : ''}`;
}

async function getFxQuote(currency) {
  if (currency === 'BRL') return { currency: 'BRL', brl_per_unit: 1, source: 'Real brasileiro', quote_time: null };
  const now = Date.now();
  const cached = fxRateCache.get(currency);
  if (cached && cached.expires > now) return cached.quote;
  if (fxRateRequests.has(currency)) return fxRateRequests.get(currency);
  const pending = api(`/api/fx-rates/${currency}`).then((quote) => {
    fxRateCache.set(currency, { quote, expires: Date.now() + (currency === 'USD' ? 15 * 60_000 : 30_000) });
    return quote;
  }).finally(() => fxRateRequests.delete(currency));
  fxRateRequests.set(currency, pending);
  return pending;
}

function conversionNotice(quotes) {
  if (!Array.isArray(quotes) || !quotes.length) return '';
  return ` Conversões usadas: ${quotes.map((quote) => `1 ${quote.currency} = ${fxRateAmount(quote.currency, quote.brl_per_unit)}`).join('; ')}.`;
}

async function updateContributionCurrencyPreview() {
  const form = $('#contribution-form');
  if (!form) return;
  const isEditing = Boolean(form.elements.id.value);
  const currency = form.elements.currency.value || 'BRL';
  const conversionFields = isEditing ? ['current_value', 'current_unit_price'] : ['invested_value', 'current_value', 'current_unit_price'];
  $('#contribution-currency-label').textContent = isEditing ? 'Moeda do valor atual' : 'Moeda de entrada';
  $('[data-money-label="invested_value"]').textContent = `Valor investido (${isEditing ? 'BRL' : currency})`;
  $('[data-money-label="current_value"]').textContent = `Valor atual deste aporte (${currency})`;
  $('[data-money-label="current_unit_price"]').textContent = `Preço atual por unidade (${currency})`;
  form.elements.invested_value.step = !isEditing && currency === 'BTC' ? '0.00000001' : '0.01';
  form.elements.current_value.step = currency === 'BTC' ? '0.00000001' : '0.01';
  form.elements.current_unit_price.step = '0.00000001';
  const hint = $('#contribution-fx-preview');
  if (currency === 'BRL') {
    hint.textContent = isEditing
      ? 'O valor investido permanece em reais. O valor atual e o preço por unidade são calculados na moeda selecionada e armazenados em reais.'
      : 'Ao criar, o valor investido é convertido uma vez para reais. O preço por unidade é calculado pelo valor atual e pela quantidade.';
    conversionFields.forEach((name) => {
      $(`[data-money-preview="${name}"]`).textContent = name === 'current_unit_price'
        ? 'Calculado pelo valor atual dividido pela quantidade.'
        : '';
    });
    return;
  }
  hint.textContent = 'Consultando cotação…';
  try {
    const quote = await getFxQuote(currency);
    if (form.elements.currency.value !== currency) return;
    hint.textContent = `${fxQuoteDescription(quote)}. ${isEditing
      ? 'O valor investido fica em reais; o valor atual e o preço por unidade usam a moeda selecionada e serão convertidos ao salvar.'
      : 'O valor investido será convertido uma vez ao criar e ficará em reais.'} O preço por unidade é calculado pelo valor atual e pela quantidade.`;
    conversionFields.forEach((name) => {
      const input = form.elements[name];
      const value = input.value === '' ? null : Number(input.value);
      $(`[data-money-preview="${name}"]`).textContent = value == null || !Number.isFinite(value)
        ? (name === 'current_unit_price' ? 'Calculado pelo valor atual dividido pela quantidade.' : '')
        : `≈ ${fxAmount(value * quote.brl_per_unit)} armazenados em BRL`;
    });
  } catch (error) {
    if (form.elements.currency.value === currency) hint.textContent = error.message;
  }
}

function updateContributionUnitPrice() {
  const form = $('#contribution-form');
  const units = Number(form.elements.units.value);
  const currentValue = Number(form.elements.current_value.value);
  form.elements.current_unit_price.value = Number.isFinite(units) && units > 0 && Number.isFinite(currentValue)
    ? (currentValue / units).toFixed(8).replace(/\.?0+$/, '')
    : '';
}

async function changeContributionCurrency() {
  const form = $('#contribution-form');
  const previous = form.dataset.inputCurrency || 'BRL';
  const next = form.elements.currency.value || 'BRL';
  if (previous === next) {
    await updateContributionCurrencyPreview();
    return;
  }
  const isEditing = Boolean(form.elements.id.value);
  const fields = isEditing ? ['current_value'] : ['invested_value', 'current_value'];
  try {
    const [fromQuote, toQuote] = await Promise.all([getFxQuote(previous), getFxQuote(next)]);
    if (form.elements.currency.value !== next) return;
    const precision = next === 'BTC' ? 8 : 2;
    for (const name of fields) {
      const input = form.elements[name];
      const value = input.value === '' ? null : Number(input.value);
      if (value == null || !Number.isFinite(value)) continue;
      input.value = (value * fromQuote.brl_per_unit / toQuote.brl_per_unit).toFixed(precision);
    }
    form.dataset.inputCurrency = next;
    updateContributionUnitPrice();
    await updateContributionCurrencyPreview();
  } catch (error) {
    form.elements.currency.value = previous;
    form.dataset.inputCurrency = previous;
    await updateContributionCurrencyPreview();
    notify(error.message, true);
  }
}

async function updateQuoteRowPreview(row) {
  const currency = $('[data-quote-currency]', row)?.value || 'BRL';
  const input = $('.quote-value', row);
  const preview = $('.quote-conversion-preview', row);
  if (currency === 'BRL') {
    preview.textContent = 'Armazenado em BRL';
    return;
  }
  preview.textContent = 'Consultando cotação…';
  try {
    const quote = await getFxQuote(currency);
    if ($('[data-quote-currency]', row)?.value !== currency) return;
    const value = input.value === '' ? null : Number(input.value);
    preview.textContent = value == null || !Number.isFinite(value)
      ? fxQuoteDescription(quote)
      : `≈ ${fxAmount(value * quote.brl_per_unit)} · ${fxQuoteDescription(quote)}`;
  } catch (error) {
    preview.textContent = error.message;
  }
}

async function saveQuoteRow(button) {
  const row = button.closest('tr');
  const input = $('.quote-value', row);
  if (!input.reportValidity()) return;
  const item = quoteRowPayload(row);
  button.disabled = true;
  button.textContent = 'Salvando…';
  button.classList.remove('is-saved');
  $$('input, select', row).forEach((control) => { control.disabled = true; });
  try {
    const result = await api('/api/holdings/current-values', { method: 'PUT', body: JSON.stringify({ items: [item] }) });
    await refreshDashboard();
    row.dataset.dirty = '0';
    button.textContent = 'Salvo';
    button.classList.add('is-saved');
    const label = quoteRowLabel(row);
    notify(`${label} atualizado.${conversionNotice(result.conversions)}`);
  } catch (error) {
    button.textContent = 'Salvar';
    notify(error.message, true);
  } finally {
    $$('input, select', row).forEach((control) => { control.disabled = false; });
    button.disabled = false;
  }
}

function quoteRowPayload(row) {
  return {
    holding_id: Number(row.dataset.holdingId),
    contribution_id: row.dataset.contributionId ? Number(row.dataset.contributionId) : null,
    current_value: Number($('.quote-value', row).value),
    currency: $('[data-quote-currency]', row).value
  };
}

function quoteRowLabel(row) {
  const name = $('.title-cell', row).textContent.trim();
  const aporte = $('.quote-contribution-cell', row).textContent.trim();
  return aporte ? `${name} · ${aporte}` : name;
}

function renderDashboard(data) {
  dashboardData = data;
  const summary = data.summary;
  $('#positions-title').textContent = activeBrokerage ? `Corretora: ${activeBrokerage}` : 'Minha carteira';
  $('#brokerage-back-button').hidden = !activeBrokerage;
  $('#back-to-positions').textContent = activeBrokerage ? `← ${activeBrokerage}` : '← Minha carteira';
  $('#back-from-quotes').textContent = activeBrokerage ? `← ${activeBrokerage}` : '← Minha carteira';
  $('#import-agora-button').hidden = Boolean(activeBrokerage && activeBrokerage.toLocaleLowerCase('pt-BR') !== 'ágora');
  $('#add-button').innerHTML = activeBrokerage
    ? '<span aria-hidden="true">＋</span> Adicionar produto'
    : '<span aria-hidden="true">＋</span> Produto';
  $('#as-of').textContent = displayDate(data.as_of);
  $('#invested-total').textContent = money(summary.invested_total);
  $('#current-total').textContent = money(summary.current_total);
  $('#gain-total').textContent = money(summary.gain_total);
  $('#gain-total').classList.toggle('positive', summary.gain_total >= 0);
  $('#gain-total').classList.toggle('negative', summary.gain_total < 0);
  $('#return-total').textContent = `${pct(summary.return_pct)} de retorno sobre o total investido`;
  $('#projected-next-day').textContent = money(summary.projected_next_day_total);
  $('#projected-30d').textContent = money(summary.projected_30d_total);
  $('#projected-360d').textContent = money(summary.projected_360d_total);
  const projectionUnit = summary.projection_basis === 'calendar' ? 'dias corridos' : 'dias úteis';
  $('#projection-days-next').textContent = summary.projected_next_day_total == null && summary.elapsed_business_days < 5
    ? `Histórico insuficiente (${summary.elapsed_business_days}/5 dias)`
    : 'Passo seguinte da fórmula';
  $('#projection-days').textContent = summary.projection_basis === 'mixed'
    ? 'Projeção indisponível para carteira mista'
    : summary.projected_30d_total == null && summary.elapsed_business_days < 30
      ? `Histórico insuficiente (${summary.elapsed_business_days}/30 dias)`
      : `${summary.projection_days_30} ${projectionUnit} contados no período`;
  $('#projection-days-year').textContent = summary.projection_basis === 'mixed'
    ? 'Projeção indisponível para carteira mista'
    : summary.projected_360d_total == null && summary.elapsed_business_days < 90
      ? `Histórico insuficiente (${summary.elapsed_business_days}/90 dias)`
      : `${summary.projection_days_360} ${projectionUnit} contados no período`;
  if (summary.undated_positions_count) {
    $('#projection-days').textContent += ` · ${summary.undated_positions_count} aporte(s) sem data`;
  }
  $('#holdings-count').textContent = `${summary.holdings_count} ${summary.holdings_count === 1 ? 'posição cadastrada' : 'posições cadastradas'}`;
  $('#calendar-footnote').textContent = data.holiday_count;

  const body = $('#holdings-body');
  $('#holdings-summary').innerHTML = '';
  if (!data.holdings.length) {
    body.innerHTML = '<tr><td colspan="13" class="empty-state">Sua carteira ainda está vazia.</td></tr>';
    $('#empty-callout').hidden = false;
    $('#empty-add-button').hidden = !canEditPositions();
    return;
  }
  $('#empty-callout').hidden = true;
  body.innerHTML = data.holdings.map((item) => {
    const returnClass = item.return_pct == null ? '' : item.return_pct >= 0 ? 'positive' : 'negative';
    return `<tr>
      <td><button class="investment-open open-brokerage" type="button" data-brokerage="${escapeHtml(item.brokerage)}" title="Ver produtos de ${escapeHtml(item.brokerage)}">${escapeHtml(item.brokerage)}</button></td>
      <td class="title-cell" title="${escapeHtml(item.name)}"><button class="investment-open open-contributions" type="button" data-id="${item.id}">${escapeHtml(item.name)}</button><small class="investment-count">${item.contribution_count} ${item.contribution_count === 1 ? 'aporte' : 'aportes'} · ver detalhes</small></td>
      <td>${escapeHtml(item.asset_type_label)}</td>
      <td class="date-cell">${displayDate(item.purchase_date)}</td>
      <td class="numeric">${money(item.invested_value)}</td>
      <td class="numeric">${money(item.current_value)}</td>
      <td class="numeric ${returnClass}">${pct(item.return_pct)}</td>
      <td class="numeric">${pct(item.daily_rate)}</td>
      <td class="numeric">${projectionCell(item.projected_30d_return_pct, item.elapsed_days, 30, pct)}</td>
      <td class="numeric">${projectionCell(item.projected_360d_return_current_pct, item.elapsed_days, 90, pct)}</td>
      <td class="numeric">${projectionCell(item.projected_next_day_value, item.elapsed_days, 5, money)}</td>
      <td class="numeric">${projectionCell(item.projected_30d_value, item.elapsed_days, 30, money)}</td>
      <td class="numeric">${projectionCell(item.projected_360d_value, item.elapsed_days, 90, money)}</td>
    </tr>`;
  }).join('');
  const totalReturnClass = summary.return_pct == null ? '' : summary.return_pct >= 0 ? 'positive' : 'negative';
  $('#holdings-summary').innerHTML = `<tr class="portfolio-total-row">
    <th colspan="4">Totais${activeBrokerage ? ` · ${escapeHtml(activeBrokerage)}` : ''} · data média ${displayDate(summary.average_purchase_date)}</th>
    <td class="numeric">${money(summary.invested_total)}</td>
    <td class="numeric">${money(summary.current_total)}</td>
    <td class="numeric ${totalReturnClass}">${pct(summary.return_pct)}</td>
    <td class="numeric">${pct(summary.daily_rate)}</td>
    <td class="numeric">${projectionCell(summary.projected_30d_return_pct, summary.elapsed_business_days, 30, pct)}</td>
    <td class="numeric">${projectionCell(summary.projected_360d_return_pct, summary.elapsed_business_days, 90, pct)}</td>
    <td class="numeric">${projectionCell(summary.projected_next_day_total, summary.elapsed_business_days, 5, money)}</td>
    <td class="numeric">${projectionCell(summary.projected_30d_total, summary.elapsed_business_days, 30, money)}</td>
    <td class="numeric">${projectionCell(summary.projected_360d_total, summary.elapsed_business_days, 90, money)}</td>
  </tr>`;
  $$('.open-brokerage').forEach((button) => button.addEventListener('click', async () => {
    activeBrokerage = button.dataset.brokerage;
    try {
      await refreshDashboard();
      $('#positions-view').scrollIntoView({ block: 'start', behavior: 'smooth' });
    } catch (error) { notify(error.message, true); }
  }));
  $$('.open-contributions').forEach((button) => button.addEventListener('click', () => openContributions(Number(button.dataset.id))));
}

async function refreshDashboard() {
  const query = activeBrokerage ? `?brokerage=${encodeURIComponent(activeBrokerage)}` : '';
  const data = await api(`/api/dashboard${query}`);
  renderDashboard(data);
}

async function refreshHolidays() {
  const data = await api('/api/holidays?limit=24');
  $('#holiday-count').textContent = `${data.total} datas no calendário`;
  const list = $('#holiday-list');
  if (!data.items.length) {
    list.innerHTML = '<li class="muted">Nenhum feriado futuro cadastrado.</li>';
    return;
  }
  list.innerHTML = data.items.map((item) => `<li>
    <span>${displayDate(item.date)} · ${escapeHtml(item.name)}</span>
    ${canManageCalendar() ? `<button class="holiday-remove" type="button" title="Remover ${escapeHtml(item.name)}" data-date="${item.date}" data-name="${escapeHtml(item.name)}">Remover</button>` : ''}
  </li>`).join('');
  $$('.holiday-remove').forEach((button) => button.addEventListener('click', async () => {
    if (!window.confirm(`Remover ${button.dataset.name} de ${displayDate(button.dataset.date)}?`)) return;
    try {
      await api(`/api/holidays/${button.dataset.date}`, { method: 'DELETE' });
      await Promise.all([refreshHolidays(), refreshDashboard()]);
      notify('Feriado removido.');
    } catch (error) { notify(error.message, true); }
  }));
}

function openHoldingDialog(id = null, brokerageContext = null) {
  const dialog = $('#holding-dialog');
  const form = $('#holding-form');
  const item = id == null ? null : dashboardData?.holdings.find((holding) => holding.id === id);
  form.reset();
  form.elements.id.value = item?.id ?? '';
  form.elements.brokerage.readOnly = !item && Boolean(brokerageContext);
  form.elements.brokerage.value = item?.brokerage ?? brokerageContext ?? 'Não informada';
  form.elements.asset_type.value = item?.asset_type ?? 'titulo_publico';
  form.elements.name.value = item?.name ?? '';
  form.elements.tax_note.value = item?.tax_note ?? '';
  $('#holding-dialog-title').textContent = item ? 'Editar produto' : 'Novo produto';
  $('#holding-tax-note-wrap').hidden = !item;
  $('#holding-form-hint').textContent = item
    ? 'O produto identifica corretora, tipo e nome. Os aportes são mantidos separadamente.'
    : brokerageContext
      ? `Este produto será cadastrado na corretora ${brokerageContext}. Depois você registra o aporte individual.`
      : 'O produto identifica apenas corretora, tipo e nome. Depois você registra o aporte individual.';
  $('#save-product-button').textContent = item ? 'Salvar alterações' : 'Salvar produto';
  $('#delete-holding-button').hidden = !item;
  dialog.showModal();
}

function openProductPicker(brokerageContext = activeBrokerage) {
  const body = $('#product-list tbody');
  const products = (dashboardData?.holdings || []).filter((item) => !brokerageContext
    || item.brokerage.trim().toLocaleLowerCase('pt-BR') === brokerageContext.trim().toLocaleLowerCase('pt-BR'));
  const dialog = $('#product-dialog');
  dialog.dataset.brokerage = brokerageContext || '';
  $('#product-picker-title').textContent = brokerageContext ? `Produtos · ${brokerageContext}` : 'Selecione um produto';
  $('#product-picker-hint').textContent = brokerageContext
    ? `Escolha um produto desta corretora para registrar outro aporte, ou cadastre um produto na ${brokerageContext}.`
    : 'Escolha um produto para registrar outro aporte ou cadastre um produto novo.';
  $('#product-picker-brokerage-header').hidden = Boolean(brokerageContext);
  $('#new-product-button').textContent = brokerageContext
    ? `＋ Cadastrar produto na ${brokerageContext}`
    : '＋ Cadastrar novo produto';
  if (!products.length) {
    body.innerHTML = `<tr><td colspan="${brokerageContext ? 3 : 4}" class="empty-state">${brokerageContext ? `Ainda não há produtos na ${escapeHtml(brokerageContext)}.` : 'Ainda não há produtos cadastrados.'}</td></tr>`;
  } else {
    body.innerHTML = products.map((item) => `<tr>
      <td${brokerageContext ? ' hidden' : ''}>${escapeHtml(item.brokerage)}</td>
      <td>${escapeHtml(item.asset_type_label)}</td>
      <td class="title-cell">${escapeHtml(item.name)}</td>
      <td><button class="action-link select-product" type="button" data-id="${item.id}">Selecionar</button></td>
    </tr>`).join('');
    $$('.select-product').forEach((button) => button.addEventListener('click', async () => {
      const holdingId = Number(button.dataset.id);
      $('#product-dialog').close();
      if (await openContributions(holdingId)) openContributionDialog();
    }));
  }
  $('#product-dialog').showModal();
}

async function openContributions(holdingId) {
  try {
    contributionData = await api(`/api/holdings/${holdingId}/contributions`);
    renderContributions(contributionData);
    $('#positions-view').hidden = true;
    $('#contributions-view').hidden = false;
    $('#contributions-view').scrollIntoView({ block: 'start', behavior: 'smooth' });
    return true;
  } catch (error) { notify(error.message, true); return false; }
}

function showPositionsView() {
  $('#contributions-view').hidden = true;
  $('#quotes-view').hidden = true;
  $('#positions-view').hidden = false;
  contributionData = null;
}

function renderContributions(data) {
  const holding = data.holding;
  const summary = data.summary;
  $('#contributions-title').textContent = holding.name;
  $('#contributions-subtitle').textContent = `${holding.brokerage} · ${holding.asset_type_label} · ${data.items.length} ${data.items.length === 1 ? 'aporte' : 'aportes'} · referência ${displayDate(holding.as_of)}`;
  const body = $('#contributions-body');
  if (!data.items.length) {
    body.innerHTML = '<tr><td colspan="15" class="empty-state">Ainda não há aportes. Adicione o primeiro aporte para registrar a posição.</td></tr>';
  } else {
    body.innerHTML = data.items.map((item) => `<tr>
      <td class="date-cell">${displayDate(item.purchase_date)}</td>
      <td class="numeric">${unitsFormat.format(item.units)}</td>
      <td class="numeric">${money(item.invested_value)}</td>
      <td class="numeric">${money(item.current_value)}</td>
      <td class="numeric ${item.return_pct == null ? '' : item.return_pct >= 0 ? 'positive' : 'negative'}">${pct(item.return_pct)}</td>
      <td class="numeric">${pct(item.daily_rate)}</td>
      <td class="numeric">${item.elapsed_days || '—'}</td>
      <td class="numeric">${projectionCell(item.projected_next_day_value, item.elapsed_days, 5, money)}</td>
      <td class="numeric">${projectionCell(item.projected_30d_value, item.elapsed_days, 30, money)}</td>
      <td class="numeric">${projectionCell(item.projected_30d_return_pct, item.elapsed_days, 30, pct)}</td>
      <td class="numeric">${projectionCell(item.projected_360d_value, item.elapsed_days, 90, money)}</td>
      <td class="numeric">${projectionCell(item.projected_360d_return_invested_pct, item.elapsed_days, 90, pct)}</td>
      <td class="numeric">${projectionCell(item.projected_360d_return_current_pct, item.elapsed_days, 90, pct)}</td>
      <td class="date-cell">${displayDate(item.maturity_date)}</td>
      <td>${canEditPositions() ? `<button class="action-link edit-contribution" type="button" data-id="${item.id}">Editar</button>` : ''}</td>
    </tr>`).join('');
    $$('.edit-contribution').forEach((button) => button.addEventListener('click', () => openContributionDialog(Number(button.dataset.id))));
  }
  $('#contributions-summary').innerHTML = `<tr class="contribution-total-row">
    <th>Totais · data média ${displayDate(summary.purchase_date)}</th>
    <td class="numeric">${unitsFormat.format(summary.units)}</td>
    <td class="numeric">${money(summary.invested_value)}</td>
    <td class="numeric">${money(summary.current_value)}</td>
    <td class="numeric">${pct(summary.return_pct)}</td>
    <td class="numeric">${pct(summary.daily_rate)}</td>
    <td class="numeric">${summary.elapsed_days || '—'}</td>
    <td class="numeric">${projectionCell(summary.projected_next_day_value, summary.elapsed_days, 5, money)}</td>
    <td class="numeric">${projectionCell(summary.projected_30d_value, summary.elapsed_days, 30, money)}</td>
    <td class="numeric">${projectionCell(summary.projected_30d_return_pct, summary.elapsed_days, 30, pct)}</td>
    <td class="numeric">${projectionCell(summary.projected_360d_value, summary.elapsed_days, 90, money)}</td>
    <td class="numeric">${projectionCell(summary.projected_360d_return_invested_pct, summary.elapsed_days, 90, pct)}</td>
    <td class="numeric">${projectionCell(summary.projected_360d_return_current_pct, summary.elapsed_days, 90, pct)}</td>
    <td class="date-cell">—</td><td></td>
  </tr>`;
  $('#edit-parent-button').hidden = !canEditPositions();
  $('#add-contribution-button').hidden = !canEditPositions();
}

function openContributionDialog(contributionId = null) {
  const form = $('#contribution-form');
  const item = contributionId == null ? null : contributionData?.items.find((contribution) => contribution.id === contributionId);
  form.reset();
  form.dataset.inputCurrency = 'BRL';
  form.elements.id.value = item?.id ?? '';
  form.elements.purchase_date.value = item?.purchase_date ?? '';
  form.elements.maturity_date.value = item?.maturity_date ?? '';
  form.elements.units.value = item?.units ?? '';
  form.elements.invested_value.value = item?.invested_value ?? '';
  form.elements.current_value.value = item?.current_value ?? '';
  form.elements.current_unit_price.value = '';
  form.elements.reference_date.value = item?.reference_date ?? localTodayString();
  form.elements.tax_note.value = item?.tax_note ?? '';
  updateContributionUnitPrice();
  $('#contribution-dialog-title').textContent = item ? 'Editar aporte' : 'Novo aporte';
  $('#delete-contribution-button').hidden = !item;
  $('#contribution-dialog').showModal();
  updateContributionCurrencyPreview();
}

function localTodayString() {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}

async function openQuotesView() {
  if (!dashboardData?.holdings.length) {
    notify('Cadastre um investimento antes de atualizar os valores.', true);
    return;
  }
  $('#quote-list').innerHTML = '<tr><td colspan="6" class="empty-state">Carregando aportes individuais…</td></tr>';
  $('#positions-view').hidden = true;
  $('#contributions-view').hidden = true;
  $('#quotes-view').hidden = false;
  $('#quotes-view').scrollIntoView({ block: 'start', behavior: 'smooth' });
  try {
    const data = await api('/api/holdings/current-values');
    $('#quote-list').innerHTML = data.items.map((item) => {
      const aporte = item.contribution_id
        ? `#${item.contribution_number} · ${displayDate(item.purchase_date)}`
        : 'Sem aporte cadastrado';
      return `<tr data-holding-id="${item.holding_id}" data-contribution-id="${item.contribution_id ?? ''}" data-dirty="0">
        <td>${escapeHtml(item.brokerage)}</td>
        <td class="title-cell">${escapeHtml(item.name)}</td>
        <td class="quote-contribution-cell">${escapeHtml(aporte)}</td>
        <td class="numeric quote-input-cell"><input class="quote-value" type="number" min="0" step="0.01" required aria-label="Valor atual de ${escapeHtml(item.name)} · ${escapeHtml(aporte)}" value="${Number(item.current_value).toFixed(2)}"></td>
        <td class="quote-currency-cell"><select data-quote-currency aria-label="Moeda de entrada para ${escapeHtml(item.name)} · ${escapeHtml(aporte)}"><option value="BRL">BRL</option><option value="USD">USD</option><option value="BTC">BTC</option></select><small class="quote-conversion-preview">Armazenado em BRL</small></td>
        <td class="quote-row-actions"><button class="button button-secondary save-quote-row" type="button">Salvar</button></td>
      </tr>`;
    }).join('') || '<tr><td colspan="6" class="empty-state">Nenhum aporte encontrado.</td></tr>';
    $$('#quote-list tr[data-holding-id]').forEach((row) => updateQuoteRowPreview(row));
  } catch (error) {
    $('#quote-list').innerHTML = '<tr><td colspan="6" class="empty-state">Não foi possível carregar os aportes.</td></tr>';
    notify(error.message, true);
  }
}

function downloadBackup() {
  window.location.assign('/api/backup');
}

function isAdmin() { return currentUser?.role === 'admin'; }
function canEditPositions() { return isAdmin() || Boolean(currentUser?.permissions?.can_edit_positions); }
function canManageCalendar() { return isAdmin() || Boolean(currentUser?.permissions?.can_manage_calendar); }
function canManageBackups() { return isAdmin() || Boolean(currentUser?.permissions?.can_manage_backups); }

function applyUserPermissions() {
  const permissions = currentUser?.permissions || {};
  const edit = canEditPositions();
  $('#current-user-label').textContent = `${currentUser.name} · ${currentUser.username}`;
  $('#users-button').hidden = !isAdmin();
  $('#add-button').hidden = !edit;
  $('#quotes-button').hidden = !edit;
  $('#import-agora-button').hidden = !edit;
  $('#empty-add-button').hidden = !edit;
  $('#holiday-form').hidden = !canManageCalendar();
  $('#backup-button').hidden = !canManageBackups();
  $('#restore-button').hidden = !canManageBackups();
  if (!isAdmin() && !permissions.can_view_portfolio) {
    $('.intro').hidden = true;
    $('.summary-grid').hidden = true;
    $('.portfolio-panel').hidden = true;
    $('.calendar-panel').hidden = !canManageCalendar();
    $('#limited-access').hidden = false;
    notify('Sua conta não tem permissão para visualizar a carteira.', true);
  }
}

function openChangePassword(forced = false) {
  const dialog = $('#change-password-dialog');
  if (dialog.open) return;
  $('#change-password-title').textContent = forced ? 'Defina uma nova senha' : 'Trocar senha';
  $('#change-password-close').hidden = forced;
  $('#change-password-cancel').hidden = forced;
  dialog.dataset.forced = forced ? '1' : '0';
  $('#change-password-form').reset();
  dialog.showModal();
  $('#change-password-form').elements.current_password.focus();
}

function permissionLabel(key) {
  return ({
    can_view_portfolio: 'Visualizar carteira', can_edit_positions: 'Editar posições',
    can_manage_calendar: 'Gerenciar calendário', can_manage_backups: 'Gerenciar backups'
  })[key];
}

async function refreshUsers() {
  const data = await api('/api/admin/users');
  const users = data.items;
  $('#user-list').innerHTML = `<div class="table-wrap user-table-wrap"><table class="user-table">
    <thead><tr><th>Nome</th><th>Usuário</th><th>Acesso</th><th>Permissões</th><th></th></tr></thead>
    <tbody>${users.map((user) => {
      const perms = Object.entries(user.permissions).filter(([, enabled]) => enabled).map(([key]) => permissionLabel(key));
      const role = user.role === 'admin' ? 'Administrador' : (user.active ? 'Ativo' : 'Desativado');
      return `<tr><td>${escapeHtml(user.name)}</td><td>${escapeHtml(user.username)}</td><td>${role}${user.must_change_password ? ' · troca de senha pendente' : ''}</td><td>${user.role === 'admin' ? 'Todas' : escapeHtml(perms.join(', ') || 'Nenhuma')}</td><td>${user.role === 'user' ? `<button class="action-link edit-user" type="button" data-id="${user.id}">Editar</button>` : ''}</td></tr>`;
    }).join('')}</tbody></table></div>`;
  initializeSortableTables($('#user-list'));
  $$('.edit-user').forEach((button) => button.addEventListener('click', () => {
    const user = users.find((item) => item.id === Number(button.dataset.id));
    if (user) openUserEditor(user);
  }));
}

function openUserEditor(user = null) {
  const form = $('#user-editor-form');
  form.reset();
  form.elements.id.value = user?.id ?? '';
  form.elements.username.value = user?.username ?? '';
  form.elements.username.disabled = Boolean(user);
  form.elements.name.value = user?.name ?? '';
  form.elements.password.value = '';
  form.elements.password.required = !user;
  form.elements.active.checked = user?.active ?? true;
  $('#active-user-wrap').hidden = !user;
  $('#delete-user-button').hidden = !user;
  $('#user-editor-title').textContent = user ? `Editar ${user.username}` : 'Criar usuário';
  $('#user-password-hint').textContent = user
    ? 'Se informar uma senha nova, o usuário deverá trocá-la ao entrar. Mínimo de 12 caracteres.'
    : 'O usuário deverá trocar a senha ao entrar pela primeira vez. Mínimo de 12 caracteres.';
  for (const key of ['can_view_portfolio', 'can_edit_positions', 'can_manage_calendar', 'can_manage_backups']) {
    form.elements[key].checked = user ? user.permissions[key] : (key === 'can_view_portfolio');
  }
  $('#user-editor-dialog').showModal();
}

$$('[data-close]').forEach((button) => button.addEventListener('click', () => $(`#${button.dataset.close}`).close()));
$('#add-button').addEventListener('click', () => openProductPicker(activeBrokerage));
$('#empty-add-button').addEventListener('click', () => openProductPicker(activeBrokerage));
$('#new-product-button').addEventListener('click', () => {
  const brokerageContext = $('#product-dialog').dataset.brokerage || null;
  $('#product-dialog').close();
  openHoldingDialog(null, brokerageContext);
});
$('#brokerage-back-button').addEventListener('click', async () => {
  activeBrokerage = null;
  try { await refreshDashboard(); }
  catch (error) { notify(error.message, true); }
});
$('#quotes-button').addEventListener('click', openQuotesView);
$('#back-to-positions').addEventListener('click', showPositionsView);
$('#back-from-quotes').addEventListener('click', showPositionsView);
$('#edit-parent-button').addEventListener('click', () => {
  const id = contributionData?.holding?.id;
  if (!id) return;
  showPositionsView();
  openHoldingDialog(id);
});
$('#add-contribution-button').addEventListener('click', () => openContributionDialog());
$('#import-agora-button').addEventListener('click', () => $('#agora-file').click());
$('#agora-file').addEventListener('change', async (event) => {
  const file = event.target.files[0];
  event.target.value = '';
  if (!file) return;
  if (file.size > 15 * 1024 * 1024) {
    notify('O PDF deve ter no máximo 15 MB.', true);
    return;
  }
  try {
    agoraPreview = await api('/api/holdings/import/agora/preview', {
      method: 'POST', body: file, headers: { 'Content-Type': 'application/pdf' }
    });
    $('#agora-import-report').textContent = `Extrato de ${displayDate(agoraPreview.report_date)} · ${agoraPreview.items.length} posições encontradas`;
    $('#agora-import-items').innerHTML = agoraPreview.items.map((item, index) => `<div class="import-row">
      <div class="import-name"><strong>${escapeHtml(item.name)}</strong><small>Ágora · ${escapeHtml(item.asset_type_label)} · ${item.action} · referência ${displayDate(item.reference_date)}</small></div>
      <div class="import-value"><strong>${unitsFormat.format(item.units)} cotas</strong><small>${money(item.current_unit_price)} por cota</small></div>
      <div class="import-value"><strong>${money(item.invested_value)}</strong><small>atual ${money(item.current_value)}</small></div>
      <label>Data do aporte<input class="import-purchase-date" type="date" data-index="${index}" value="${item.purchase_date || ''}"></label>
    </div>`).join('');
    $('#agora-import-dialog').showModal();
  } catch (error) { notify(error.message, true); }
});
$('#agora-import-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!agoraPreview?.items?.length) return;
  const items = agoraPreview.items.map((item, index) => ({
    ...item,
    purchase_date: $(`.import-purchase-date[data-index="${index}"]`).value || null
  }));
  try {
    const result = await api('/api/holdings/import/agora/commit', {
      method: 'POST', body: JSON.stringify({ items })
    });
    $('#agora-import-dialog').close();
    agoraPreview = null;
    await refreshDashboard();
    notify(`Importação concluída: ${result.created} criadas, ${result.updated} atualizadas.`);
  } catch (error) { notify(error.message, true); }
});
$('#refresh-button').addEventListener('click', async () => {
  try { await Promise.all([refreshDashboard(), refreshHolidays()]); notify('Cálculos atualizados.'); }
  catch (error) { notify(error.message, true); }
});
$('#backup-button').addEventListener('click', downloadBackup);
$('#restore-button').addEventListener('click', () => $('#restore-file').click());
$('#users-button').addEventListener('click', async () => {
  try {
    await refreshUsers();
    $('#users-dialog').showModal();
  } catch (error) { notify(error.message, true); }
});
$('#create-user-button').addEventListener('click', () => openUserEditor());
$('#change-password-button').addEventListener('click', () => openChangePassword(false));

$('#change-password-dialog').addEventListener('cancel', (event) => {
  if ($('#change-password-dialog').dataset.forced === '1') event.preventDefault();
});

$('#change-password-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const currentPassword = form.elements.current_password.value;
  const newPassword = form.elements.new_password.value;
  if (newPassword !== form.elements.confirm_password.value) {
    notify('A confirmação da nova senha não confere.', true);
    return;
  }
  try {
    await api('/api/auth/change-password', {
      method: 'POST', body: JSON.stringify({ current_password: currentPassword, new_password: newPassword })
    });
    $('#change-password-dialog').close();
    notify('Senha alterada. Entre novamente com a nova senha.');
    setTimeout(() => window.location.assign('/login'), 700);
  } catch (error) { notify(error.message, true); }
});

$('#user-editor-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const id = form.elements.id.value;
  const password = form.elements.password.value;
  const permissions = Object.fromEntries(
    ['can_view_portfolio', 'can_edit_positions', 'can_manage_calendar', 'can_manage_backups']
      .map((key) => [key, form.elements[key].checked])
  );
  const payload = {
    username: form.elements.username.value,
    name: form.elements.name.value,
    permissions
  };
  if (password) payload.password = password;
  if (id) payload.active = form.elements.active.checked;
  try {
    await api(id ? `/api/admin/users/${id}` : '/api/admin/users', {
      method: id ? 'PUT' : 'POST', body: JSON.stringify(payload)
    });
    $('#user-editor-dialog').close();
    await refreshUsers();
    notify(id ? 'Usuário atualizado.' : 'Usuário criado. Entregue a senha inicial à pessoa com segurança.');
  } catch (error) { notify(error.message, true); }
});

$('#delete-user-button').addEventListener('click', async () => {
  const id = $('#user-editor-form').elements.id.value;
  const username = $('#user-editor-form').elements.username.value;
  if (!id || !window.confirm(`Excluir o usuário ${username}? O acesso será removido imediatamente.`)) return;
  try {
    await api(`/api/admin/users/${id}`, { method: 'DELETE' });
    $('#user-editor-dialog').close();
    await refreshUsers();
    notify('Usuário excluído.');
  } catch (error) { notify(error.message, true); }
});

$('#restore-file').addEventListener('change', async (event) => {
  const file = event.target.files[0];
  event.target.value = '';
  if (!file) return;
  try {
    const backup = JSON.parse(await file.text());
    if (!Array.isArray(backup.holdings) || !Array.isArray(backup.holidays) || backup.format_version !== 1) {
      throw new Error('Este arquivo não parece ser um backup válido do app.');
    }
    const message = `O backup substituirá ${backup.holdings.length} posições e ${backup.holidays.length} feriados atuais. Continuar?`;
    if (!window.confirm(message)) return;
    await api('/api/restore', { method: 'POST', body: JSON.stringify(backup) });
    await Promise.all([refreshDashboard(), refreshHolidays()]);
    notify('Backup restaurado.');
  } catch (error) { notify(error.message || 'Não foi possível ler o backup.', true); }
});

$('#logout-button').addEventListener('click', async () => {
  await api('/api/auth/logout', { method: 'POST' }).catch(() => null);
  window.location.assign('/login');
});

$('#holding-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const formData = new FormData(form);
  const id = formData.get('id');
  const payload = {
    name: formData.get('name'),
    brokerage: formData.get('brokerage'),
    asset_type: formData.get('asset_type'),
    tax_note: formData.get('tax_note') || ''
  };
  try {
    const result = await api(id ? `/api/holdings/${id}` : '/api/holdings', {
      method: id ? 'PUT' : 'POST', body: JSON.stringify(payload)
    });
    $('#holding-dialog').close();
    await refreshDashboard();
    if (id) {
      notify('Dados do produto atualizados.');
      await openContributions(Number(id));
    } else {
      notify('Produto cadastrado. Agora informe os dados do aporte.');
      if (await openContributions(result.id)) openContributionDialog();
    }
  } catch (error) { notify(error.message, true); }
});

$('#delete-holding-button').addEventListener('click', async () => {
  const id = $('#holding-form').elements.id.value;
  if (!id || !window.confirm('Excluir este produto e todos os seus aportes da carteira?')) return;
  try {
    await api(`/api/holdings/${id}`, { method: 'DELETE' });
    $('#holding-dialog').close();
    showPositionsView();
    await refreshDashboard();
    notify('Investimento excluído.');
  } catch (error) { notify(error.message, true); }
});

$('#contribution-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const formData = new FormData(event.currentTarget);
  const id = formData.get('id');
  const holdingId = contributionData?.holding?.id;
  if (!holdingId) return;
  const payload = {
    units: Number(formData.get('units')),
    invested_value: Number(formData.get('invested_value')),
    current_value: Number(formData.get('current_value')),
    current_unit_price: formData.get('current_unit_price') ? Number(formData.get('current_unit_price')) : null,
    currency: formData.get('currency') || 'BRL',
    purchase_date: formData.get('purchase_date') || null,
    maturity_date: formData.get('maturity_date') || null,
    reference_date: formData.get('reference_date') || null,
    tax_note: formData.get('tax_note') || ''
  };
  try {
    const result = await api(id
      ? `/api/holdings/${holdingId}/contributions/${id}`
      : `/api/holdings/${holdingId}/contributions`, {
      method: id ? 'PUT' : 'POST', body: JSON.stringify(payload)
    });
    $('#contribution-dialog').close();
    await refreshDashboard();
    await openContributions(holdingId);
    const conversion = result.currency_conversion && result.currency_conversion.currency !== 'BRL'
      ? conversionNotice([result.currency_conversion]) : '';
    notify(`${id ? 'Aporte atualizado.' : 'Aporte cadastrado.'}${conversion}`);
  } catch (error) { notify(error.message, true); }
});

$('#delete-contribution-button').addEventListener('click', async () => {
  const contributionId = $('#contribution-form').elements.id.value;
  const holdingId = contributionData?.holding?.id;
  if (!contributionId || !holdingId || !window.confirm('Excluir este aporte?')) return;
  try {
    await api(`/api/holdings/${holdingId}/contributions/${contributionId}`, { method: 'DELETE' });
    $('#contribution-dialog').close();
    await refreshDashboard();
    await openContributions(holdingId);
    notify('Aporte excluído.');
  } catch (error) { notify(error.message, true); }
});

$('#quotes-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const dirtyRows = $$('#quote-list tr[data-holding-id][data-dirty="1"]');
  if (!dirtyRows.length) {
    notify('Não há linhas alteradas para salvar.');
    return;
  }
  const items = dirtyRows.map((row) => quoteRowPayload(row));
  try {
    const result = await api('/api/holdings/current-values', { method: 'PUT', body: JSON.stringify({ items }) });
    dirtyRows.forEach((row) => {
      row.dataset.dirty = '0';
      const button = $('.save-quote-row', row);
      button.textContent = 'Salvo';
      button.classList.remove('is-dirty');
      button.classList.add('is-saved');
    });
    await refreshDashboard();
    showPositionsView();
    notify(`Valores atuais e projeções atualizados.${conversionNotice(result.conversions)}`);
  } catch (error) { notify(error.message, true); }
});

$('#contribution-form').elements.currency.addEventListener('change', changeContributionCurrency);
$('#contribution-form').addEventListener('input', (event) => {
  if (['units', 'current_value'].includes(event.target.name)) updateContributionUnitPrice();
  if (['invested_value', 'current_value', 'current_unit_price', 'units'].includes(event.target.name)) updateContributionCurrencyPreview();
});
$('#quote-list').addEventListener('change', (event) => {
  if (event.target.matches('[data-quote-currency]')) {
    const row = event.target.closest('tr');
    const saveButton = $('.save-quote-row', row);
    saveButton.textContent = 'Salvar';
    saveButton.classList.remove('is-saved');
    saveButton.classList.add('is-dirty');
    row.dataset.dirty = '1';
    const input = $('.quote-value', row);
    input.step = event.target.value === 'BTC' ? '0.00000001' : '0.01';
    updateQuoteRowPreview(row);
  }
});
$('#quote-list').addEventListener('input', (event) => {
  if (event.target.matches('.quote-value')) {
    const row = event.target.closest('tr');
    const saveButton = $('.save-quote-row', row);
    saveButton.textContent = 'Salvar';
    saveButton.classList.remove('is-saved');
    saveButton.classList.add('is-dirty');
    row.dataset.dirty = '1';
    updateQuoteRowPreview(row);
  }
});
$('#quote-list').addEventListener('click', (event) => {
  const button = event.target.closest('.save-quote-row');
  if (button) saveQuoteRow(button);
});

$('#holiday-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const formData = new FormData(event.currentTarget);
  try {
    await api('/api/holidays', { method: 'POST', body: JSON.stringify({ date: formData.get('date'), name: formData.get('name') }) });
    event.currentTarget.reset();
    await Promise.all([refreshHolidays(), refreshDashboard()]);
    notify('Feriado adicionado ao calendário.');
  } catch (error) { notify(error.message, true); }
});

async function initializeApp() {
  try {
    currentUser = await api('/api/auth/me');
    applyUserPermissions();
    if (currentUser.must_change_password) {
      openChangePassword(true);
      return;
    }
    if (!isAdmin() && !currentUser.permissions.can_view_portfolio) {
      if (canManageCalendar()) await refreshHolidays();
      return;
    }
    await Promise.all([refreshDashboard(), refreshHolidays()]);
  } catch (error) { notify(error.message, true); }
}

initializeSortableTables();
initializeApp();
