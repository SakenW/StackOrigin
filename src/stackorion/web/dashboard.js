import {focusOnboardingAccount} from '/onboarding.js';
import { showResourceOperation } from '/resource-operation.js';
import '/storage.js';
import { formattedSample, metricGroupsFor, mountHostTrend } from '/host-trend.js';

const paths = {
  // Lucide 1.21.0 SVG shapes, distributed with lucide-license.txt.
  domain: '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
  database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14a8 3 0 0 0 16 0V5M4 12a8 3 0 0 0 16 0"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M5 15v5h14v-5"/>',
  server: '<rect width="20" height="8" x="2" y="2" rx="2" ry="2"/><rect width="20" height="8" x="2" y="14" rx="2" ry="2"/><line x1="6" x2="6.01" y1="6" y2="6"/><line x1="6" x2="6.01" y1="18" y2="18"/>',
  grid: '<rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/>',
  list: '<path d="M3 5h.01"/><path d="M3 12h.01"/><path d="M3 19h.01"/><path d="M8 5h13"/><path d="M8 12h13"/><path d="M8 19h13"/>',
  layers: '<path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83z"/><path d="M2 12a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 12"/><path d="M2 17a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 17"/>',
  terminal: '<path d="M12 19h8"/><path d="m4 17 6-6-6-6"/>',
  chevron: '<path d="m9 18 6-6-6-6"/>',
  calendar: '<path d="M8 2v4"/><path d="M16 2v4"/><rect width="18" height="18" x="3" y="4" rx="2"/><path d="M3 10h18"/>',
  refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
  balance: '<path d="M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1"/><path d="M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4"/>',
  expiry: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  ssh: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
  coverage: '<path d="m19 5 3-3"/><path d="m2 22 3-3"/><path d="M6.3 20.3a2.4 2.4 0 0 0 3.4 0L12 18l-6-6-2.3 2.3a2.4 2.4 0 0 0 0 3.4Z"/><path d="M7.5 13.5 10 11"/><path d="M10.5 16.5 13 14"/><path d="m12 6 6 6 2.3-2.3a2.4 2.4 0 0 0 0-3.4l-2.6-2.6a2.4 2.4 0 0 0-3.4 0Z"/>',
  activity: '<path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/>',
  clipboard: '<rect width="8" height="4" x="8" y="2" rx="1" ry="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="m9 14 2 2 4-4"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  missing: '<path d="M10.1 2.182a10 10 0 0 1 3.8 0"/><path d="M13.9 21.818a10 10 0 0 1-3.8 0"/><path d="M17.609 3.721a10 10 0 0 1 2.69 2.7"/><path d="M2.182 13.9a10 10 0 0 1 0-3.8"/><path d="M20.279 17.609a10 10 0 0 1-2.7 2.69"/><path d="M21.818 10.1a10 10 0 0 1 0 3.8"/><path d="M3.721 6.391a10 10 0 0 1 2.7-2.69"/><path d="M6.391 20.279a10 10 0 0 1-2.69-2.7"/>',
  pause: '<rect x="14" y="3" width="5" height="18" rx="1"/><rect x="5" y="3" width="5" height="18" rx="1"/>',
  lock: '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  route: '<circle cx="6" cy="19" r="3"/><path d="M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15"/><circle cx="18" cy="5" r="3"/>',
  close: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
};
const categories = {
  balance: { label: '账务提醒', basis: '余额预警 · 费用增长', tone: 'warn' },
  expiry: { label: '资源到期', basis: '实例 · 域名 · 库存证书', tone: 'reminder' },
  ssh: { label: '公网 SSH', basis: '入站 TCP 22', tone: 'high' },
  coverage: { label: '漏查账号', basis: '部分或未查到', tone: 'missing' },
};
const statusLabels = { complete: '查完', partial: '部分完成', unavailable: '未查到', disabled: '已停用', missing_credentials: '未配置凭据', authorization_expired: '授权已过期' };
const coverageTones = {
  complete: 'success', partial: 'warn', unavailable: 'missing', disabled: 'neutral',
  missing_credentials: 'missing', authorization_expired: 'warn',
};
const stateIcons = { success: 'check', reminder: 'calendar', warn: 'alert', high: 'ssh', missing: 'missing', neutral: 'pause' };
const clock = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Shanghai' });
const $ = (id) => document.getElementById(id);
let report;
let active = 'all';
let workState = 'open';
let workAccount = '';
let linkedWorkHash = '';
let workOriginAsset;
const workLabels = {open:'待处理', exception:'已知例外', resolved:'已解决', superseded:'条件已更新'};
let exceptionItem;
let recheckItem;
let items = [];
let websites = [];
let websiteRegistrationAllowed = false;
let assetCatalog;
let assetRequest;
let domainDeliveryRequest;
let assetAccountNames = new Map();
let assetType = 'all';
let serverType = 'all';
let hostRegistrationAsset;
let hostSourcesRequest;
const assetTypes = { all: '全部', domain: '域名', server: '服务器', security_group: '安全组', certificate: '库存证书', backup: '云盘备份' };
const serverTypes = { all: '全部服务器', cloud: '云服务器', lighthouse: '轻量应用服务器' };
const assetIcons = { domain: 'domain', server: 'server', security_group: 'ssh', certificate: 'lock', backup: 'calendar' };
const inventoryExpiryLabels = { domain: '域名到期', certificate: '库存证书到期' };
const backupInventoryEmpty = {
  disabled: '尚未为腾讯云连接启用云盘备份盘点。',
  pending: '已启用云盘备份盘点。开始巡检后查看结果。',
  complete: '本轮查完，未发现绑定定期快照策略的云盘。',
  partial: '仅查到部分备份范围，暂无法确认是否存在已绑定的云盘。',
  unavailable: '本轮未取得云盘备份清单。请查看巡检覆盖。',
};

function el(tag, classname, text) {
  const node = document.createElement(tag);
  if (classname) node.className = classname;
  if (text !== undefined) node.textContent = text;
  return node;
}

function drawIcon(node, name) {
  node.setAttribute('aria-hidden', 'true');
  node.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" stroke-linecap="round">' + paths[name] + '</svg>';
  return node;
}

function icon(name) {
  const node = el('span');
  node.dataset.icon = name;
  return drawIcon(node, name);
}

function button(classname) {
  const node = el('button', classname);
  node.type = 'button';
  return node;
}

function formatTime(value) {
  return value ? clock.format(new Date(value)) + ' UTC+8' : '—';
}

function backupTime(value) {
  if (!value) return '未取得';
  return /(?:Z|[+-]\d{2}:\d{2})$/.test(value)
    ? formatTime(value) : value.replace('T', ' ') + '（厂商未提供时区）';
}

function observation(row) {
  if (row.kind === 'balance') return row.amount + ' ' + row.currency;
  if (row.kind === 'billing') return '增加 ' + row.increase + ' 元';
  if (row.kind === 'ssh') return row.source_cidr || row.source;
  if (row.kind === 'coverage') return statusLabels[row.status];
  if (!row.expires_at) return '到期时间未取得';
  const days = Math.ceil((Date.parse(row.expires_at) - Date.parse(row.data_source === 'synthetic' ? report.simulated_at : report.observed_at)) / 86400000);
  return days > 0 ? days + ' 天后到期' : '已到期';
}

function stateBadge(label, tone) {
  const badge = el('span', 'pill');
  badge.dataset.tone = tone;
  badge.append(icon(stateIcons[tone]), el('span', '', label));
  return badge;
}

function findingTone(row) {
  if (row.kind === 'coverage') return coverageTones[row.status] || 'missing';
  if (row.kind === 'billing') return 'warn';
  if (row.kind === 'expiry') {
    if (row.status === 'expired') return 'high';
    if (!row.expires_at) return 'missing';
  }
  return categories[row.kind].tone;
}

function showDetails(category, title, account, fields, note = '', observedAt = report?.observed_at, source = 'live') {
  workOriginAsset = null;
  $('detail-kind').textContent = category;
  $('detail-title').textContent = title;
  $('detail-account').textContent = account;
  $('detail-fields').replaceChildren(...fields.map(([label, value]) => {
    const line = el('div');
    line.append(el('dt', '', label), el('dd', '', value));
    return line;
  }));
  showResourceOperation(null);
  $('detail-links').replaceChildren();
  $('detail-links').hidden = true;
  domainDeliveryRequest?.abort();
  $('domain-delivery').hidden = true;
  $('asset-related-work').hidden = true;
  $('asset-related-work').open = false;
  $('detail-note').textContent = note;
  $('detail-note').hidden = !note;
  $('work-next-step').hidden = true;
  $('detail-history').hidden = true;
  $('detail-history').open = false;
  showExceptionForm(null);
  recheckItem = null;
  $('work-recheck').hidden = true;
  $('work-recheck-status').textContent = '';
  $('detail-time').textContent = formatTime(observedAt);
  $('detail-source').textContent = source === 'synthetic' ? '模拟数据' : '真实连接';
  if (!$('details').open) $('details').showModal();
}

function detailMonitorLink(section, id, label) {
  const link = el('a', 'filter', label);
  link.href = '#' + section + '?' + new URLSearchParams({target:id});
  link.addEventListener('click', (event) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    $('details').close();
    if (link.hash === location.hash) applyResourceRoute(true);
  });
  $('detail-links').append(link);
  $('detail-links').hidden = false;
}

function resourceRoute() {
  const [section, query] = location.hash.slice(1).split('?');
  return {section, id:new URLSearchParams(query).get('target')};
}

function applyResourceRoute(moveFocus = false) {
  const {section, id} = resourceRoute();
  if (!id || !['servers', 'websites'].includes(section)) return;
  const host = section === 'servers';
  const prefix = host ? 'host' : 'website';
  const target = (host ? hostRows : websites).find(row => row.id === id);
  $(prefix + '-target').value = target?.id || '';
  (host ? renderHost : renderWebsite)(target);
  $(prefix + '-status').textContent = target ? '' : '这个监控目标当前不可用，请重新选择。';
  if (moveFocus) $(prefix + '-title').focus();
}

function rememberMonitorSelection(section, select) {
  const {section: activeSection, id} = resourceRoute();
  if (activeSection === section && id) {
    history.replaceState(null, '', '#' + section + '?' + new URLSearchParams({target:select.value}));
  }
}

function checkFields(checks) {
  return Object.entries(checks).map(([label, state]) => [label, statusLabels[state]]);
}

function registrationFields(row) {
  return [['注册商', row.registrar || '未核实'],
    ['注册到期', row.expires_at ? formatTime(row.expires_at) : '未取得'],
    ['自动续费', row.auto_renew === true ? '已开启' : row.auto_renew === false ? '已关闭' : '未取得']];
}

function billingFindingFields(row) {
  return [['对比口径', row.id === 'account-total' ? '账号总消费' : '产品汇总 · ' + row.name],
    [row.prior_month, row.prior_amount + ' 元'], [row.month, row.amount + ' 元'],
    ['较前月增加', row.increase + ' 元'],
    ['涨幅', row.growth_percent === null ? '前月为零' : row.growth_percent + '%']];
}

function billingAccountFields(bill) {
  const labels = {unavailable:'未取得账单', pending:'账单尚未就绪', incomplete:'账单字段不完整'};
  const fields = [bill.prior, bill.recent].map(period => [
    period.month + ' 账号费用',
    [period.amount === null ? '' : period.amount + ' 元', labels[period.status] || ''].filter(Boolean).join(' · '),
  ]);
  fields.push(['可比产品', bill.compared.filter(id => id.startsWith('product:')).length + ' 项']);
  if (!bill.compared.includes('account-total') && bill.prior.amount !== null && bill.recent.amount !== null) {
    fields.push(['账号费用比较', '两期金额不适合计算涨幅']);
  }
  if (bill.unmatched_products) {
    fields.push(['缺少可比数据', bill.unmatched_products + ' 项 · ' + bill.unmatched_examples.join('、')]);
  }
  return fields;
}

function matchesExpiryAsset(row, entry) {
  const kind = row.resource_kind || 'server';
  return entry.kind === kind && entry.account === row.account &&
    entry.id === row.id && entry.data_source === row.data_source &&
    (kind === 'server' ? entry.region === row.region : entry.scope === row.scope);
}

function expiryAsset(row) {
  const matches = assetCatalog?.assets.filter(entry => matchesExpiryAsset(row, entry));
  return matches?.length === 1 ? matches[0] : null;
}

function linkExpiryAsset(asset) {
  if (!asset) return;
  const link = assetLink(asset, 'filter');
  link.textContent = '查看' + assetTypes[asset.kind] + '资产';
  $('detail-links').append(link);
  $('detail-links').hidden = false;
}

function showFinding(row) {
  let fields;
  let note;
  switch (row.kind) {
    case 'balance':
      fields = [['余额指标', row.metric], ['当前余额', observation(row)], ['提醒下限', row.threshold + ' ' + row.currency]];
      break;
    case 'billing':
      fields = billingFindingFields(row);
      break;
    case 'expiry':
      fields = row.resource_kind === 'domain' ? [...registrationFields(row), ['巡检结果', observation(row)]] :
        row.resource_kind === 'certificate' ? [['库存证书 ID', row.id],
          ['库存证书到期', formatTime(row.expires_at)], ['巡检结果', observation(row)]] :
        [['实例 ID', row.id], ['地区', row.region], ['计费类型', row.billing === 'prepaid' ? '预付费' : '按量计费'],
        ['到期时间', formatTime(row.expires_at)], ['巡检结果', observation(row)]];
      break;
    case 'ssh':
      fields = [['规则 ID', row.id], ['地区', row.region], ['方向 / 动作', '入站 / 允许'], ['协议 / 端口', 'TCP / ' + row.first_port],
        ['来源', row.source_cidr || row.source], ['已知实例', row.instances.join('、') || '—']];
      note = '发现放行规则。实际公网连通性未验证。';
      break;
    case 'coverage':
      fields = [['本轮范围', observation(row)], ['原因', row.reason], ['最近成功', formatTime(row.last_success)], ...checkFields(row.checks)];
      break;
  }
  showDetails(row.kind === 'billing' ? '费用增长' : categories[row.kind].label, row.name, row.account_label, fields, note,
    row.data_source === 'synthetic' ? report.simulated_at : report.observed_at, row.data_source);
  if (row.kind === 'expiry') {
    const asset = expiryAsset(row);
    linkExpiryAsset(asset);
    if (asset?.monitor_id) detailMonitorLink('servers', asset.monitor_id, '打开这台服务器的监控');
  }
  if (row.kind === 'coverage') detailConnectionLink(row.account, row.data_source);
}

function detailConnectionLink(account, source = 'live') {
  if (source !== 'live') return;
  const link = el('a', 'filter', '打开服务接入');
  link.href = '#settings?' + new URLSearchParams({account});
  link.addEventListener('click', event => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    $('details').close();
    if (location.hash === link.hash) focusOnboardingAccount();
  });
  $('detail-links').append(link);
  $('detail-links').hidden = false;
}

function selectFilter(key) {
  active = active === key ? 'all' : key;
  syncFilter();
  renderRows();
}

function syncFilter() {
  for (const node of document.querySelectorAll('[data-filter]')) {
    node.setAttribute('aria-pressed', String(node.dataset.filter === active));
  }
}

function overviewCard(key, value, description, tone = 'neutral') {
  const card = $('overview-' + key);
  card.querySelector('strong').textContent = value;
  card.querySelector('small').textContent = description;
  card.closest('article').querySelector('.metric-icon').dataset.tone = tone;
  const bar = card.querySelector('.overview-bar');
  if (bar) bar.hidden = true;
}

function overviewMonitors(key, targets) {
  if (!targets.length) {
    overviewCard(key, '0', '未登记监控目标');
    return;
  }
  const web = key === 'web';
  const complete = targets.filter(target => target.history[0]?.status === 'pass').length;
  const failed = targets.filter(target => target.history[0]?.status === 'fail').length;
  const missing = targets.length - complete - failed;
  const simulated = targets.filter(target => target.source === 'synthetic').length;
  const summary = [complete + (web ? ' 正常' : ' 采集完整')];
  if (failed) summary.push(failed + ' 异常');
  if (missing) summary.push(missing + ' 未取得完整结果');
  const scheduled = targets.filter(target => target.schedule?.enabled).length;
  if (scheduled) summary.push(scheduled + ' 定时');
  if (simulated) summary.push('含 ' + simulated + ' 模拟');
  overviewCard(key, targets.length, summary.join(' · '), failed ? 'high' : missing ? 'missing' : 'success');
  const bar = $('overview-' + key).querySelector('.overview-bar');
  bar.replaceChildren(...Object.entries({success:complete, high:failed, missing})
    .filter(([, count]) => count)
    .map(([tone, count]) => {
      const part = el('span');
      part.dataset.tone = tone;
      part.style.flexGrow = count;
      return part;
    }));
  bar.hidden = false;
}

function renderMetrics() {
  $('metrics').replaceChildren(...Object.entries(categories).map(([key, category]) => {
    const card = button('metric');
    card.dataset.filter = key;
    const top = el('span', 'metric-top', category.label);
    const mark = el('span', 'metric-icon');
    const count = key === 'balance' && report ? (report.counts.balance ?? 0) + (report.counts.billing ?? 0) : report?.counts[key];
    if (count) mark.dataset.tone = category.tone;
    mark.append(icon(key));
    top.append(mark);
    const bottom = el('span', 'metric-bottom');
    bottom.append(el('strong', '', String(count ?? '—')), el('small', '', category.basis));
    card.append(top, bottom);
    card.disabled = !report;
    card.addEventListener('click', () => {
      selectFilter(key);
      $('findings-title').focus();
    });
    return card;
  }));
}

function renderFilters() {
  $('filters').replaceChildren(...Object.entries({ all: { label: '全部' }, ...categories }).map(([key, category]) => {
    const control = button('filter');
    control.textContent = category.label;
    control.dataset.filter = key;
    control.addEventListener('click', () => selectFilter(key));
    return control;
  }));
}

function renderRows() {
  const visible = report.findings.filter((row) => active === 'all' || row.kind === active ||
    (active === 'balance' && row.kind === 'billing'));
  $('finding-summary').textContent = visible.length + ' 项发现';
  const rows = visible.map((row) => {
    const category = row.kind === 'billing' ? categories.balance : categories[row.kind];
    const control = button('row');
    const lead = el('span', 'row-main');
    const mark = el('span', 'row-icon');
    const tone = findingTone(row);
    mark.dataset.tone = tone;
    mark.append(icon(assetIcons[row.resource_kind] || (row.kind === 'billing' ? 'balance' : row.kind)));
    const label = el('span');
    label.append(el('strong', '', row.name), el('small', '', (inventoryExpiryLabels[row.resource_kind] ||
      (row.kind === 'billing' ? '费用增长' : row.kind === 'balance' ? '余额预警' : category.label)) +
      ' · ' + row.account_label + (row.data_source === 'synthetic' ? ' · 模拟' : '')));
    lead.append(mark, label);
    const result = el('span', 'result', observation(row));
    result.dataset.tone = tone;
    control.append(lead, result, icon('chevron'));
    control.addEventListener('click', () => showFinding(row));
    return control;
  });
  $('rows').replaceChildren(...(rows.length ? rows : [el('p', 'empty', active === 'all' ? '本轮无风险或漏查' : '本轮无此类发现')]));
}

function renderCoverage() {
  const { complete, partial, unavailable, disabled } = report.coverage;
  const expected = report.expected_accounts;
  const breakdown = { complete, partial, unavailable };
  const caption = el('div', 'chart-caption');
  caption.append(el('strong', '', expected ? complete + ' / ' + expected : '—'),
    el('small', '', '查完 / 应查账号'));
  const bar = el('div', 'coverage-bar');
  bar.setAttribute('aria-hidden', 'true');
  for (const [state, count] of Object.entries(breakdown)) {
    if (!count) continue;
    const segment = el('span');
    segment.dataset.tone = coverageTones[state];
    segment.style.flexGrow = count;
    bar.append(segment);
  }
  $('coverage-chart').replaceChildren(caption, bar);
  $('coverage-legend').replaceChildren(...Object.entries(breakdown).map(([state, count]) => {
    const item = el('li');
    const tone = coverageTones[state];
    const mark = icon(stateIcons[tone]);
    mark.dataset.tone = tone;
    item.append(mark, el('span', '', statusLabels[state]), el('b', '', String(count)));
    return item;
  }));
  $('disabled').textContent = '停用 ' + disabled;
  $('accounts').replaceChildren(...report.accounts.map((account) => {
    const control = button('account');
    const state = stateBadge(statusLabels[account.status], coverageTones[account.status] || 'missing');
    control.append(el('span', '', account.label + (account.data_source === 'synthetic' ? ' · 模拟' : '')), state);
    control.addEventListener('click', () => {
      showDetails('账号覆盖', account.label, account.provider, [
        ['账号标识', account.id], ['本轮范围', state.textContent], ...checkFields(account.checks),
        ...(account.balance ? [['余额指标', account.balance.metric],
          ['当前余额', account.balance.amount + ' ' + account.balance.currency]] : []),
        ...(account.billing_checks || []).flatMap(billingAccountFields),
        ...(account.status === 'partial' || account.status === 'unavailable' ?
          [['最近成功', formatTime(account.last_success)]] : []),
      ], account.status === 'partial' || account.status === 'unavailable' ? account.reason : '',
        account.data_source === 'synthetic' ? report.simulated_at : report.observed_at, account.data_source);
      const work = button('filter');
      work.textContent = '查看此账号的待办';
      work.addEventListener('click', () => {
        workAccount = accountFilterKey(account.id, account.data_source);
        workState = 'open';
        $('details').close();
        renderWorkItems();
        $('work-account').focus();
      });
      $('detail-links').append(work);
      $('detail-links').hidden = false;
      if (account.status === 'partial' || account.status === 'unavailable') {
        detailConnectionLink(account.id, account.data_source);
      }
    });
    return control;
  }));
}

function workItemState(item) {
  return item.exception?.active ? 'exception' : item.status;
}

function accountFilterKey(account, source) {
  return JSON.stringify([account, source === 'synthetic']);
}

function disambiguateAccountLabels(accounts) {
  const counts = new Map();
  for (const {label} of accounts.values()) counts.set(label, (counts.get(label) || 0) + 1);
  return new Map([...accounts].map(([key, {id, label}]) =>
    [key, counts.get(label) === 1 ? label : label + ' · ' + id]));
}

function renderWorkAccounts() {
  const accounts = new Map();
  function add(id, label, source) {
    const key = accountFilterKey(id, source);
    if (accounts.has(key)) return;
    const name = label + (source === 'synthetic' ? ' · 模拟' : '');
    accounts.set(key, {id, label:name});
  }
  for (const account of report?.accounts || []) add(account.id, account.label, account.data_source);
  for (const {evidence} of items) add(evidence.account, evidence.account_label, evidence.data_source);
  if (!accounts.has(workAccount)) workAccount = '';
  const select = $('work-account');
  const names = disambiguateAccountLabels(accounts);
  const options = [...names].map(([key, label]) => new Option(label, key));
  options.sort((left, right) => left.text.localeCompare(right.text, 'zh-CN'));
  select.replaceChildren(new Option('全部账号和来源', ''), ...options);
  select.value = workAccount;
  $('work-account-filter').hidden = !accounts.size;
  return names;
}

$('work-account').addEventListener('change', event => {
  workAccount = event.target.value;
  renderWorkItems();
});

function renderWorkItems() {
  const accountNames = renderWorkAccounts();
  const counts = {open:0, exception:0, resolved:0, superseded:0};
  const scoped = {...counts};
  const visible = [];
  let simulated = 0;
  for (const item of items) {
    const state = workItemState(item);
    counts[state]++;
    if (state === 'open' && item.evidence.data_source === 'synthetic') simulated++;
    if (!workAccount || workAccount === accountFilterKey(item.evidence.account, item.evidence.data_source)) {
      scoped[state]++;
      visible.push(item);
    }
  }
  const {open:pending, exception:exceptions, resolved, superseded} = counts;
  const labels = Object.fromEntries(Object.entries(workLabels).filter(([state]) =>
    (state !== 'superseded' || superseded) && (state !== 'exception' || exceptions)));
  if (!labels[workState]) workState = 'open';
  const known = exceptions ? ' · ' + exceptions + ' 项已知例外' : '';
  overviewCard('work', pending, resolved + ' 已解决' + known + (superseded ? ' · ' + superseded + ' 条件已更新' : '') +
    (simulated ? ' · ' + simulated + ' 项模拟待办' : ''), pending ? 'warn' : 'neutral');
  const summary = scoped.open + ' 项待处理 · ' + scoped.resolved + ' 项已解决' +
    (scoped.exception ? ' · ' + scoped.exception + ' 项已知例外' : '') +
    (scoped.superseded ? ' · ' + scoped.superseded + ' 项条件已更新' : '');
  if ($('work-summary').textContent !== summary) $('work-summary').textContent = summary;
  $('work-filters').replaceChildren(...Object.entries(labels).map(([state, label]) => {
    const filter = button('filter');
    filter.textContent = label;
    filter.setAttribute('aria-pressed', String(workState === state));
    filter.addEventListener('click', () => {
      workState = state;
      renderWorkItems();
      $('work-filters').querySelector('[aria-pressed=true]').focus();
    });
    return filter;
  }));
  const matching = visible.filter(item => workItemState(item) === workState);
  $('work-rows').replaceChildren(...(matching.length ? matching.map((item) => {
    const row = button('row');
    const finding = item.evidence;
    const title = el('span', 'row-main');
    const mark = el('span', 'row-icon');
    const findingIcon = assetIcons[finding.resource_kind] ||
      { billing: 'balance', website: 'domain', 'website-certificate': 'lock', 'host-disk': 'server' }[finding.kind] || finding.kind;
    mark.append(icon(findingIcon));
    const label = el('span');
    const findingLabel = inventoryExpiryLabels[finding.resource_kind] ||
      {billing: '费用增长', balance: '余额预警', 'host-disk': '磁盘偏满',
        'website-certificate': certificateLabels[finding.status]}[finding.kind] || categories[finding.kind]?.label;
    const accountName = accountNames.get(accountFilterKey(finding.account, finding.data_source));
    label.append(el('strong', '', finding.name),
      el('small', '', (findingLabel || '网站异常') + ' · ' + accountName));
    title.append(mark, label);
    const state = workItemState(item);
    row.append(title, stateBadge(labels[state], {resolved:'success', open:'warn'}[state] || 'neutral'), icon('chevron'));
    row.addEventListener('click', () => showWorkItem(row, item.id));
    return row;
  }) : [el('p', 'empty', (workAccount ? '此账号或来源' : '') + '没有' + labels[workState] + '事项')]));
}

async function showWorkItem(trigger, key, {hash = '', asset = null} = {}) {
  const isCurrent = () => trigger.isConnected && (!hash || location.hash === hash);
  trigger.setAttribute('aria-busy', 'true');
  try {
    const response = await fetch(withMode('/api/work-items/' + key));
    if (!response.ok) throw new Error('work item request failed');
    const item = await response.json();
    if (!isCurrent()) return;
    linkedWorkHash = hash;
    renderWorkItem(item, asset);
    if (asset) $('detail-title').focus();
  } catch {
    if (isCurrent()) {
      const status = $(hash ? 'work-route-status' : 'error');
      status.textContent = hash ? '暂时打不开关联待办，请从待办列表查找。' : '待办详情读取失败，请重试。';
      status.hidden = false;
    }
  } finally {
    trigger.removeAttribute('aria-busy');
  }
}

function applyWorkItemRoute() {
  const hash = location.hash;
  const [section, query] = hash.split('?');
  $('work-route-status').textContent = '';
  if (section !== '#work-items' || !query) return;
  const key = new URLSearchParams(query).get('item');
  if (!key || !/^[a-f0-9]{64}$/.test(key)) {
    $('work-route-status').textContent = '待办编号无效，请从待办列表查找。';
    return;
  }
  $('work-title').focus();
  showWorkItem($('work-title'), key, {hash});
}

$('details').addEventListener('close', () => {
  if ($('details').open) return;
  if (routedAsset && location.hash === routedAsset) {
    history.replaceState(null, '', '#assets');
    focusAssetTrigger(routedAsset);
    routedAsset = '';
  }
  if (linkedWorkHash && location.hash === linkedWorkHash) {
    window.history.replaceState(null, '', '#work-items');
  }
  linkedWorkHash = '';
});

function showExceptionForm(item) {
  exceptionItem = item;
  const editor = $('work-exception');
  const form = $('work-exception-form');
  editor.open = false;
  editor.hidden = !item;
  form.elements.reason.value = item?.exception?.reason ?? '';
  form.elements.review_on.value = item?.exception?.review_on ?? '';
  $('work-exception-remove').hidden = !item?.exception;
  $('work-exception-status').textContent = '';
  for (const control of form.elements) control.disabled = false;
}

async function saveWorkException(remove) {
  const item = exceptionItem;
  if (!item) return;
  const form = $('work-exception-form');
  const status = $('work-exception-status');
  const detailUrl = '/api/work-items/' + item.id;
  const showMock = $('show-mock').checked;
  const fields = remove ? {remove:true} : {
    reason:form.elements.reason.value.trim(), review_on:form.elements.review_on.value,
  };
  for (const control of form.elements) control.disabled = true;
  status.textContent = '正在保存…';
  try {
    const response = await fetch(detailUrl + '/exception', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({show_mock:showMock, ...fields}),
    });
    if (!response.ok) {
      if (item === exceptionItem) status.textContent = {
        400:'请填写例外原因，并选择晚于今天（UTC+8）的复查日期。',
        404:'找不到这条待办，请返回列表重新打开。',
        409:'这条待办已关闭，请返回列表查看最新状态。',
      }[response.status] || '例外保存失败，请重试。';
      return;
    }
    if (item === exceptionItem && $('details').open) {
      status.textContent = '已保存，正在刷新详情…';
      const detail = await fetch(withMode(detailUrl));
      if (detail.ok) {
        const updated = await detail.json();
        if (item === exceptionItem && $('details').open) {
          renderWorkItem(updated, workOriginAsset);
          if (updated.status === 'open') {
            $('work-exception').open = true;
            status.textContent = remove ? '例外已取消，已恢复待处理。' : '例外已保存。';
            $('work-exception').querySelector('summary').focus();
          }
        }
      } else if (item === exceptionItem) {
        status.textContent = '例外已保存，请重新打开详情查看。';
      }
    }
    await refreshWorkItems();
  } catch {
    if (item === exceptionItem) status.textContent = '更新未能确认，请重新打开待办核对。';
  } finally {
    if (item === exceptionItem) for (const control of form.elements) control.disabled = false;
  }
}

$('work-exception-form').addEventListener('submit', event => {
  event.preventDefault();
  saveWorkException(false);
});
$('work-exception-remove').addEventListener('click', () => saveWorkException(true));

const recheckScenarios = $('work-recheck-scenario');

function showWorkRecheck(item) {
  const disk = item.evidence.kind === 'host-disk';
  recheckItem = item;
  $('work-recheck').hidden = false;
  $('work-recheck-title').textContent = disk ? '磁盘复查' : '网站复查';
  $('work-recheck-controls').hidden = !item.can_recheck;
  $('work-recheck-demo').hidden = item.evidence.data_source !== 'synthetic';
  const source = $(disk ? 'host-scenario' : 'website-scenario');
  recheckScenarios.replaceChildren(...Array.from(source.options, option => option.cloneNode(true)));
  recheckScenarios.value = disk ? 'high' : item.evidence.kind === 'website-certificate' ? 'certificate-due' : 'mismatch';
  recheckScenarios.disabled = $('work-recheck-run').disabled = false;
  $('work-recheck-run').textContent = '立即复查';
}

function describeWorkRecheck(item, result, label) {
  let signal;
  if (item.evidence.kind === 'host-disk') {
    const {disk_percent: usage, threshold} = result;
    signal = usage === null ? '根分区使用率未取得' : '根分区 ' + usage + '% · ' +
      (usage >= threshold ? '达到' : '低于') + '预警阈值 ' + threshold + '%';
  } else if (item.evidence.kind === 'website-certificate') {
    signal = certificateLabels[result.certificate_status];
  } else {
    signal = [websiteStates[result.status], 'HTTP ' + (result.http_status ?? '未取得'), contentLabels[result.content]].join(' · ');
  }
  return label + ' ' + formatTime(result.observed_at) + ' · ' + signal + ' · 待办' + workLabels[workItemState(item)];
}

$('work-recheck-run').addEventListener('click', async () => {
  const item = recheckItem;
  const showMock = $('show-mock').checked;
  const runButton = $('work-recheck-run');
  const status = $('work-recheck-status');
  const selectedScenario = recheckScenarios.value;
  const isCurrent = () => recheckItem === item && $('details').open;
  runButton.disabled = recheckScenarios.disabled = true;
  runButton.textContent = '复查中…';
  status.textContent = '';
  try {
    const response = await fetch('/api/work-items/' + item.id + '/recheck', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({show_mock:showMock, scenario:selectedScenario}),
    });
    if (!response.ok) {
      if (isCurrent()) status.textContent = response.status === 409 || response.status === 404
        ? '待办或监控设置已变化，请重新打开待办。' : '未能确认复查结果，请查看监控历史后再试。';
      if (response.status === 409 || response.status === 404) await refreshWorkItems();
      return;
    }
    const {result, target, item: updated} = await response.json();
    if (showMock === $('show-mock').checked) {
      if (item.evidence.kind === 'host-disk') {
        hostRows = hostRows.map(row => row.id === target.id ? target : row);
        overviewMonitors('host', hostRows);
        if ($('host-target').value === target.id) renderHost(target);
      } else {
        websites = websites.map(row => row.id === target.id ? target : row);
        overviewMonitors('web', websites);
        if ($('website-target').value === target.id) renderWebsite(target);
      }
    }
    if (isCurrent()) {
      const historyOpen = $('detail-history').open;
      renderWorkItem(updated, workOriginAsset);
      $('detail-history').open = historyOpen;
      recheckScenarios.value = selectedScenario;
      status.textContent = describeWorkRecheck(updated, result, '本次复查');
      if (updated.can_recheck) runButton.focus();
      else status.focus();
    }
    await refreshWorkItems();
  } catch {
    if (isCurrent()) status.textContent = '未能确认复查结果，请查看监控历史后再试。';
  } finally {
    if (isCurrent()) {
      runButton.disabled = recheckScenarios.disabled = false;
      runButton.textContent = '立即复查';
    }
  }
});

function renderWorkItem(item, originAsset = null) {
  const finding = item.evidence;
  const fields = [
    ['状态', workLabels[workItemState(item)]],
    ['账号标识', finding.account],
    ['待办编号', item.id],
    ['首次发现', formatTime(item.first_seen)],
    ['最近命中', formatTime(item.last_seen)],
  ];
  if (item.exception) fields.push(['例外原因', item.exception.reason],
    ['复查日期（UTC+8）', item.exception.review_on + (item.exception.active ? '' : ' · 已到期')]);
  if (item.operation_binding) fields.push(['资源操作编号', item.operation_binding.id]);
  if (item.resolved_at) fields.push([item.status === 'superseded' ? '条件更新时间' : '解决时间', formatTime(item.resolved_at)]);
  switch (finding.kind) {
    case 'balance':
      fields.push(['余额指标', finding.metric],
        ['上次命中余额', finding.amount + ' ' + finding.currency],
        ['提醒下限', finding.threshold + ' ' + finding.currency]);
      break;
    case 'billing':
      fields.push(...billingFindingFields(finding));
      break;
    case 'expiry':
      if (finding.resource_kind === 'domain') fields.push(...registrationFields(finding));
      else if (finding.resource_kind === 'certificate') {
        fields.push(['库存证书 ID', finding.id], ['库存证书到期', formatTime(finding.expires_at)]);
      }
      else fields.push(['实例 ID', finding.id], ['地区', finding.region], ['到期时间', formatTime(finding.expires_at)]);
      break;
    case 'ssh':
      fields.push(['规则 ID', finding.id], ['地区', finding.region],
        ['端口范围', finding.first_port === finding.last_port ? String(finding.first_port) : finding.first_port + '–' + finding.last_port],
        ['来源', finding.source_cidr || finding.source], ['已知实例', finding.instances.join('、') || '—']);
      break;
    case 'website':
      fields.push(['失败 URL', finding.name]);
      fields.push(['上次 HTTP 状态', finding.http_status ?? '未取得'],
        ['内容校验', contentLabels[finding.content] || '未取得']);
      break;
    case 'website-certificate':
      fields.push(['上次证书状态', certificateLabels[finding.status]],
        ['线上证书到期', formatTime(finding.expires_at)], ['当时剩余', finding.days_remaining + ' 天'],
        ['签发者', finding.issuer || '未取得'], ['TLS 版本', finding.protocol || '未取得']);
      break;
    case 'host-disk':
      fields.push(['最近异常占用', finding.used_percent + '%'], ['预警阈值', finding.threshold + '%']);
      break;
    case 'coverage':
      fields.push(['上次漏查原因', finding.reason], ...checkFields(finding.checks));
      break;
  }
  if (item.failed_observation) {
    fields.push(...websiteObservationFields(item.failed_observation).filter(([label]) =>
      !['HTTP 状态', '内容校验'].includes(label)));
  }
  let note = finding.kind === 'website' && !item.failed_observation
    ? '完整检测记录已不可用。这里保留了待办命中时的 HTTP 和内容校验结论。' : '';
  if (finding.kind === 'host-disk' && item.status === 'open' && item.monitor_target && !item.can_recheck) {
    note = '磁盘预警阈值已调整，请打开这台服务器的监控查看当前阈值。';
  }
  showDetails('巡检待办', finding.name, finding.account_label, fields, note, item.last_seen, finding.data_source);
  workOriginAsset = originAsset;
  if (item.status === 'open') showWorkItemNextStep(finding);
  if (originAsset) {
    const back = button('filter');
    back.textContent = '返回资产详情';
    back.addEventListener('click', () => {
      showAssetDetails(originAsset);
      $('asset-related-work').open = true;
      const trigger = $('asset-related-rows').querySelector('[data-work-item="' + item.id + '"]');
      (trigger || $('detail-title')).focus();
    });
    $('detail-links').append(back);
    $('detail-links').hidden = false;
  } else if (finding.kind === 'expiry') {
    linkExpiryAsset(expiryAsset(finding));
  }
  if (item.operation_binding) showResourceOperation(item.operation_binding, item.id);
  if (item.monitor_target) {
    const website = finding.kind === 'website' || finding.kind === 'website-certificate';
    detailMonitorLink(website ? 'websites' : 'servers', item.monitor_target, website ? '打开这个 URL 的监控' : '打开这台服务器的监控');
  }
  if (item.can_recheck || item.latest_detection) {
    showWorkRecheck(item);
    if (item.latest_detection) {
      $('work-recheck-status').textContent = describeWorkRecheck(
        item, item.latest_detection, finding.kind === 'host-disk' ? '最近采集' : '最近检测');
    }
  }

  showExceptionForm(item.status === 'open' ? item : null);
  const eventLabels = { exception_set:'保存例外', exception_removed:'取消例外', opened: '首次发现', confirmed: '再次命中', retained: '漏查保留', resolved: '确认解决', reopened: '重新发现', superseded:'条件已更新' };
  $('history-count').textContent = item.history.length + ' 条';
  $('history-events').replaceChildren(...item.history.map((entry) => {
    const line = el('li');
    const time = el('time', '', formatTime(entry.observed_at));
    time.dateTime = entry.observed_at;
    line.append(el('strong', '', eventLabels[entry.event]), time, el('small', '', entry.detail));
    return line;
  }));
  $('detail-history').hidden = false;
}

function showWorkItemNextStep(finding) {
  const text = {
    balance:'在对应账号核对可用余额和近期扣费，需要时安排充值。',
    billing:'在对应账号的账单中按账期和产品核对用量及费用变化。',
    coverage:'在服务接入页核对连接。按上面的漏查原因检查凭据、权限或服务状态，再运行巡检。',
    ssh:'在对应安全组核对规则和关联实例，确认管理所需的来源范围后再调整并复查。',
    expiry:{
      domain:'在注册商核对该域名的续费方式和到期日，处理后重新巡检。',
      certificate:'在上游核对这张库存证书的续签或替换计划。网站实际使用的证书可在网站监控查看。',
      server:'在服务商控制台核对该实例的续费设置和到期日。',
    }[finding.resource_kind || 'server'],
  }[finding.kind];
  if (!text) return;
  $('work-next-step').textContent = text;
  $('work-next-step').hidden = false;
  if (finding.kind === 'coverage') detailConnectionLink(finding.account, finding.data_source);
}

function renderAssets() {
  if (!assetCatalog) return;
  const all = assetCatalog.assets;
  const accounts = new Map(all.map(asset => [
    accountFilterKey(asset.account, asset.data_source),
    {id:asset.account, label:asset.account_label + (asset.data_source === 'synthetic' ? ' · 模拟' : '')},
  ]));
  assetAccountNames = disambiguateAccountLabels(accounts);
  const account = $('asset-account');
  const selectedAccount = account.value;
  const options = [...assetAccountNames].map(([key, label]) => new Option(label, key));
  options.sort((left, right) => left.text.localeCompare(right.text, 'zh-CN'));
  account.replaceChildren(new Option('全部账号', ''), ...options);
  account.value = accounts.has(selectedAccount) ? selectedAccount : '';
  account.disabled = $('asset-search').disabled = false;
  $('server-filters').hidden = assetType !== 'server';

  function filters(element, types, selected, choose) {
    element.replaceChildren(...Object.entries(types).map(([key, label]) => {
      const filter = button('filter');
      filter.textContent = label;
      filter.setAttribute('aria-pressed', String(selected === key));
      filter.addEventListener('click', () => {
        choose(key);
        renderAssets();
        element.querySelector('[aria-pressed=true]').focus();
      });
      return filter;
    }));
  }

  filters($('asset-filters'), assetTypes, assetType, (key) => {
    assetType = key;
    serverType = 'all';
  });
  filters($('server-filters'), serverTypes, serverType, (key) => { serverType = key; });
  if (assetType === 'server') {
    const monitorLink = el('a', 'filter asset-monitor-link', '服务器监控');
    monitorLink.href = '#servers';
    monitorLink.append(icon('chevron'));
    $('server-filters').append(monitorLink);
  }
  renderAssetRows();
}

function renderAssetRows() {
  if (!assetCatalog) return;
  const all = assetCatalog.assets;
  const normalize = value => String(value ?? '').normalize('NFKC').toLowerCase();
  const query = normalize($('asset-search').value).trim();
  const terms = query ? query.split(/\s+/) : [];
  const account = $('asset-account').value;
  const filtered = query || account;
  const classified = all.filter(asset => (assetType === 'all' || asset.kind === assetType)
    && (assetType !== 'server' || serverType === 'all' || asset.server_type === serverType));
  const visible = classified.filter(asset => {
    if (account && account !== accountFilterKey(asset.account, asset.data_source)) return false;
    if (!terms.length) return true;
    const fields = [asset.name, asset.id, asset.account, asset.account_label, asset.region, asset.provider,
      asset.product, asset.registrar, asset.dns_provider, asset.disk_id].map(normalize);
    return terms.every(term => fields.some(field => field.includes(term)));
  });
  const diagnostic = assetType === 'backup' && !classified.length && backupInventoryEmpty[assetCatalog.backup_inventory_status];
  $('asset-clear-filters').disabled = !filtered && assetType === 'all';
  $('asset-summary').textContent = '显示 ' + visible.length + ' 项 · 共 ' + all.length + ' 项资产';
  if (diagnostic) $('asset-summary').textContent = diagnostic;
  $('asset-rows').replaceChildren(...(visible.length ? visible.map((asset) => {
    const kind = asset.kind === 'server' ? serverTypes[asset.server_type] : assetTypes[asset.kind];
    const row = assetLink(asset, 'row');
    const main = el('span', 'row-main');
    const mark = el('span', 'row-icon');
    mark.append(icon(assetIcons[asset.kind]));
    const label = el('span');
    const accountName = assetAccountNames.get(accountFilterKey(asset.account, asset.data_source));
    label.append(el('strong', '', asset.name), el('small', '',
      [accountName, asset.region, asset.inventory_retained ? '待复核' : null].filter(Boolean).join(' · ')));
    main.append(mark, label);
    row.append(main, el('span', 'result', kind), icon('chevron'));
    return row;
  }) : diagnostic ? [] : [el('p', 'empty', filtered ? '没有符合当前筛选条件的资产' : '没有此类资产')]));
}

$('asset-search').addEventListener('input', event => {
  if (!event.isComposing) renderAssetRows();
});
$('asset-search').addEventListener('compositionend', renderAssetRows);
$('asset-account').addEventListener('change', renderAssetRows);
$('asset-clear-filters').addEventListener('click', () => {
  $('asset-search').value = $('asset-account').value = '';
  assetType = serverType = 'all';
  renderAssets();
  $('asset-search').focus();
});

function domainName(value) {
  return value.toLowerCase().replace(/\.$/, '');
}

function showDomainDelivery(asset) {
  if (asset.data_source !== 'live' || !asset.service_id) return;
  const section = $('domain-delivery');
  const trigger = $('domain-delivery-check');
  const status = $('domain-delivery-status');
  const results = $('domain-delivery-results');
  status.textContent = '';
  results.hidden = true;
  trigger.disabled = false;
  section.hidden = false;
  trigger.onclick = async () => {
    const request = domainDeliveryRequest = new AbortController();
    trigger.disabled = true;
    status.textContent = '正在核对 ' + asset.name + ' 的公网解析，这可能需要几十秒。';
    results.hidden = true;
    try {
      const response = await fetch('/api/domain-delivery/check', {
        method:'POST', headers:{'Content-Type':'application/json'}, signal:request.signal,
        body:JSON.stringify({service_id:asset.service_id, domain:asset.name}),
      });
      if (request.signal.aborted) return;
      if (!response.ok) {
        status.textContent = response.status === 404 ? '当前资产或连接不可用，请核对服务接入并重新巡检。' : '未能完成核对，请稍后重试。';
        return;
      }
      const result = await response.json();
      if (request.signal.aborted) return;
      const ns = result.recursive_ns;
      const authority = result.authority;
      const https = result.https;
      const fields = [
        [result.provider + ' 上次盘点的 NS', result.provider_nameservers.join('、') || '未取得'],
        ['平台盘点时间', formatTime(result.provider_observed_at)],
        ['本机解析器返回的 NS', ns?.nameservers.join('、') || (ns ? '未返回 NS · ' + ns.rcode : '未取得')],
        ['NS 对照', ns?.matches_provider === true ? '一致' : ns?.matches_provider === false ? '与盘点记录不同' : '无法比较'],
        ['权威应答（抽查一台）', authority?.status === 'observed' ?
          authority.nameserver + ' · ' + (authority.authoritative && authority.zone_soa_present ? '已返回权威 SOA' :
            '未取得权威 SOA（' + authority.rcode + '）') : '未取得'],
        ['HTTPS 根路径（IPv4）', https?.status === 'observed' ? 'HTTP ' + https.http_status + ' · TLS 主机名已验证' :
          https?.reason?.startsWith('no public IPv4') ? '未取得公网 IPv4' : '未取得 HTTPS 结果'],
      ];
      results.replaceChildren(...fields.map(([label, value]) => {
        const row = el('div');
        row.append(el('dt', '', label), el('dd', '', value));
        return row;
      }));
      results.hidden = false;
      status.textContent = (result.status !== 'observed' || authority?.status !== 'observed' || https?.status !== 'observed' ?
        '部分结果未取得' : '已完成本次核对') + ' · ' + formatTime(result.observed_at);
    } catch {
      if (!request.signal.aborted) status.textContent = '未能完成核对，请稍后重试。';
    } finally {
      if (!request.signal.aborted) trigger.disabled = false;
    }
  };
}

function renderAssetRelatedWork(asset, monitors) {
  if (!['server', 'domain', 'certificate'].includes(asset.kind)) return;
  const urls = new Set(monitors.map(monitor => monitor.url));
  const inventoryIdentity = {...asset, resource_kind:asset.kind};
  const uniqueAsset = assetCatalog?.assets.filter(row => matchesExpiryAsset(inventoryIdentity, row)).length === 1;
  const uniqueDomain = asset.kind === 'domain' && assetCatalog?.assets.filter(row => row.kind === 'domain' &&
    row.data_source === asset.data_source && domainName(row.name) === domainName(asset.name)).length === 1;
  const related = items.filter(item => {
    const finding = item.evidence;
    if ((finding.data_source || 'live') !== asset.data_source) return false;
    if (finding.kind === 'expiry') return uniqueAsset && matchesExpiryAsset(finding, asset);
    if (finding.kind === 'host-disk') return asset.kind === 'server' && finding.id === asset.monitor_key;
    return uniqueDomain && ['website', 'website-certificate'].includes(finding.kind) && urls.has(finding.name);
  });
  related.sort((first, second) => Number(second.status === 'open') - Number(first.status === 'open') ||
    Date.parse(second.last_seen) - Date.parse(first.last_seen));
  const pending = related.filter(item => workItemState(item) === 'open').length;
  $('asset-related-count').textContent = related.length + ' 项 · ' + pending + ' 待处理';
  $('asset-related-empty').hidden = related.length > 0;
  $('asset-related-rows').replaceChildren(...related.map(item => {
    const finding = item.evidence;
    const names = {expiry:inventoryExpiryLabels[finding.resource_kind] || '实例到期',
      'host-disk':'磁盘偏满', website:'网站异常', 'website-certificate':'线上证书'};
    const line = el('li');
    const trigger = button('row');
    trigger.dataset.workItem = item.id;
    const label = el('span', 'row-main');
    label.append(el('strong', '', names[finding.kind]), el('small', '',
      (finding.kind.startsWith('website') ? finding.name + ' · ' : '') + formatTime(item.last_seen)));
    const state = workItemState(item);
    trigger.append(label, stateBadge(workLabels[state], {resolved:'success', open:'warn'}[state] || 'neutral'));
    trigger.addEventListener('click', () => showWorkItem(trigger, item.id, {asset}));
    line.append(trigger);
    return line;
  }));
  $('asset-related-work').hidden = false;
}

function assetLink(asset, classname) {
  if (asset.route_id) {
    const link = el('a', classname);
    link.href = '#assets?' + new URLSearchParams({asset:asset.route_id});
    return link;
  }
  const trigger = button(classname);
  trigger.addEventListener('click', () => {
    showAssetDetails(asset);
    $('detail-title').focus();
  });
  return trigger;
}

function showAssetDetails(asset) {
  const kind = asset.kind === 'server' ? serverTypes[asset.server_type] : assetTypes[asset.kind];
  const fields = [['资产 ID', asset.id], ['资产类型', kind], ['账号标识', asset.account]];
  if (asset.kind === 'domain') {
    fields.push(...registrationFields(asset), ['DNS 服务商', asset.dns_provider || '未核实']);
  } else if (asset.kind === 'certificate') {
    fields.push(['库存证书到期', asset.expires_at ? formatTime(asset.expires_at) : '未取得']);
  } else if (asset.kind === 'backup') {
    fields.push(['地域', asset.region], ['云盘 ID', asset.disk_id], ['定期策略 ID', asset.policy_id],
      ['策略状态', asset.active === true ? '已启用' : asset.active === false ? '未运行' : '未取得'],
      ['下次触发', asset.active === false ? '策略未运行' : backupTime(asset.next_trigger)],
      ['最近成功自动快照', asset.snapshot_id ||
        (asset.snapshot_status === 'not_found' ? '未查到成功快照' : '未取得')],
      ['成功快照创建时间', backupTime(asset.snapshot_created_at)],
      ['恢复演练', '未取得']);
  } else {
    fields.push(['地区', asset.region || '未取得']);
    if (asset.kind === 'server') {
      fields.push(['厂商产品', asset.product], ['计费类型', ({ prepaid:'预付费', postpaid:'按量计费' })[asset.billing] || '未取得']);
      if (asset.expires_at) fields.push(['到期时间', formatTime(asset.expires_at)]);
    } else {
      fields.push(['已知实例', asset.instances.join('、') || '—']);
    }
  }
  const domains = asset.kind === 'domain' ? websites.filter((target) => {
    if ((asset.data_source === 'synthetic') !== (target.source === 'synthetic')) return false;
    const domain = domainName(asset.name);
    const hostname = domainName(new URL(target.url).hostname);
    return hostname === domain || hostname.endsWith('.' + domain);
  }) : [];
  if (asset.kind === 'domain') fields.push(['关联网站监控', String(domains.length)]);
  let note = asset.kind === 'server' && !asset.monitor_id ? '未找到与此资源身份匹配的监控目标。' : '';
  if (asset.kind === 'domain' && asset.data_source === 'live' && !asset.service_id &&
      (asset.registrar === 'Spaceship' || asset.dns_provider === 'Cloudflare')) {
    note = '重新巡检后，可从此处检查公网解析。';
  }
  if (asset.inventory_retained) {
    fields.push(['盘点状态', '最近一次检查未重新核实，保留上次观测']);
  }
  showDetails('资源详情', asset.name, asset.account_label, fields, note, asset.observed_at ?? null, asset.data_source);
  if (asset.kind === 'domain') showDomainDelivery(asset);
  showResourceOperation(asset.operation_preview);
  renderAssetRelatedWork(asset, domains);
  if (asset.monitor_id) detailMonitorLink('servers', asset.monitor_id, '打开这台服务器的监控');
  if (asset.kind === 'server' && asset.data_source === 'live' && !asset.monitor_id) {
    const add = button('monitor-secondary');
    add.textContent = '添加服务器监控';
    add.addEventListener('click', () => {
      $('details').close();
      location.hash = 'servers';
      hostRegistrationAsset = asset;
      const registration = $('host-registration');
      if (registration.open) loadHostSources();
      registration.open = true;
      $('host-registration-label').focus();
    });
    $('detail-links').append(add);
    $('detail-links').hidden = false;
  }
  for (const target of domains) detailMonitorLink('websites', target.id, target.url);
  if (asset.kind === 'domain' && asset.data_source === 'live' && websiteRegistrationAllowed) {
    const add = button('monitor-secondary');
    add.textContent = '添加网站监控';
    add.addEventListener('click', () => {
      $('details').close();
      location.hash = 'websites';
      $('website-registration-form').reset();
      $('website-registration-status').textContent = '';
      $('website-registration').open = true;
      const input = $('website-registration-url');
      input.value = 'https://' + asset.name.replace(/\.$/, '') + '/';
      input.focus();
    });
    $('detail-links').append(add);
    $('detail-links').hidden = false;
  }
}

let routedAsset = '';

function focusAssetTrigger(hash) {
  const trigger = [...$('asset-rows').querySelectorAll('a')].find(link => link.hash === hash);
  (trigger || $('asset-search')).focus();
}

function applyAssetRoute() {
  const hash = location.hash;
  const [section, search] = hash.split('?');
  const params = new URLSearchParams(search);
  const id = params.get('asset');
  const binding = params.get('operation');
  const requested = section === '#assets' && (id !== null || binding !== null);
  if (requested && !assetCatalog) return;
  const matches = requested ? assetCatalog.assets.filter(asset => id !== null ?
    asset.route_id === id : asset.operation_preview?.id === binding) : [];
  const [resource] = matches;
  if (matches.length !== 1) {
    $('asset-route-status').textContent = requested ? '对应资源当前不可用，或无法唯一匹配。请从资产列表查找。' : '';
    if (requested) $('assets').scrollIntoView();
    if (routedAsset) {
      $('details').close();
      if (section === '#assets' && !requested) focusAssetTrigger(routedAsset);
    }
    routedAsset = '';
    return;
  }
  $('asset-route-status').textContent = '';
  if (routedAsset === hash && $('details').open) return;
  routedAsset = hash;
  linkedWorkHash = '';
  showAssetDetails(resource);
  $('detail-title').focus();
}

async function loadAssets() {
  assetRequest?.abort();
  const request = new AbortController();
  assetRequest = request;
  $('assets').ariaBusy = 'true';
  try {
    const response = await fetch(withMode('/api/assets'), {signal:request.signal});
    if (!response.ok) throw new Error('asset request failed');
    const result = await response.json();
    if (request.signal.aborted) return;
    assetCatalog = result;
    const assets = assetCatalog.assets;
    const breakdown = ['server', 'domain', 'security_group'].map(key =>
      assets.filter(asset => asset.kind === key).length + ' ' + assetTypes[key]);
    const certificateCount = assets.filter(asset => asset.kind === 'certificate').length;
    if (certificateCount) breakdown.push(certificateCount + ' 库存证书');
    const backupCount = assets.filter(asset => asset.kind === 'backup').length;
    if (backupCount) breakdown.push(backupCount + ' 云盘备份');
    const sample = assets.filter(asset => asset.data_source === 'synthetic').length;
    if (sample) breakdown.push('含 ' + sample + ' 模拟');
    overviewCard('assets', assetCatalog.snapshot_at || assets.length ? assets.length : '—',
      assetCatalog.snapshot_at || assets.length ? breakdown.join(' · ') : '等待首次巡检');
    renderAssets();
    applyAssetRoute();
  } catch {
    if (request.signal.aborted) return;
    assetCatalog = undefined;
    overviewCard('assets', '—', '读取失败', 'missing');
    $('asset-search').disabled = $('asset-account').disabled = $('asset-clear-filters').disabled = true;
    $('asset-summary').textContent = '读取资产失败';
    const retry = button('filter asset-retry');
    retry.textContent = '重试';
    retry.addEventListener('click', loadAssets);
    $('asset-rows').replaceChildren(retry);
  } finally {
    if (!request.signal.aborted) $('assets').ariaBusy = 'false';
  }
}

function renderInspectionStart(snapshot) {
  const setup = snapshot.inspection_setup;
  if (!setup) return;
  const demoAvailable = snapshot.development && $('show-mock').checked;
  const hasResults = Boolean(snapshot.report?.expected_accounts);
  $('run').hidden = !setup.configured && !demoAvailable;
  $('run').lastChild.textContent = hasResults ? '重新巡检' : '开始巡检';
  $('inspection-start').hidden = setup.configured && hasResults;
  $('inspection-setup-link').hidden = setup.configured || !setup.editable;
  if (setup.configured) {
    $('inspection-start-title').textContent = '可以开始首次巡检';
    $('inspection-start-description').textContent = '选择「开始巡检」，读取已接入账号的资源和状态。';
  } else {
    $('inspection-start-title').textContent = '尚无已启用的巡检服务';
    $('inspection-start-description').textContent = setup.editable
      ? '接入一个只读服务后，就可以在工作台开始巡检。'
      : '当前使用外部服务目录。请在该目录中登记并启用支持巡检的服务。';
  }
}

function renderSnapshot(snapshot) {
  report = snapshot.report;
  items = snapshot.items;
  renderInspectionStart(snapshot);
  renderMetrics();
  renderWorkItems();
  const scopeNote = $('inspection-scope-note');
  scopeNote.hidden = !report?.selected_accounts;
  scopeNote.textContent = report?.selected_accounts ?
    '本轮选定 ' + report.selected_accounts.length + ' 个账号，应查 ' + report.expected_accounts +
      ' 个。资产清单优先显示最近一次全账号盘点。' : '';
  if (!report) {
    $('observed').textContent = '暂无巡检记录';
    $('observed').removeAttribute('datetime');
    $('finding-summary').textContent = '等待首次巡检';
    $('overview-coverage').firstChild.textContent = '账号覆盖 · 暂无巡检记录';
    return;
  }
  const {complete, partial, unavailable, disabled} = report.coverage;
  const coverage = $('overview-coverage');
  coverage.firstChild.textContent = report.expected_accounts
    ? '账号覆盖 ' + complete + ' / ' + report.expected_accounts + ' 查完' +
      (partial + unavailable ? ' · ' + (partial + unavailable) + ' 未查全' : '')
    : disabled ? '账号覆盖 · ' + disabled + ' 已停用' : '账号覆盖 · 无应查账号';
  coverage.dataset.tone = partial + unavailable ? 'missing' : 'neutral';
  renderFilters();
  syncFilter();
  renderRows();
  renderCoverage();
  $('observed').dateTime = report.observed_at;
  $('observed').textContent = formatTime(report.observed_at);
}

const phaseLabels = { dns: 'DNS 解析', tcp: 'TCP 连接', tls: 'TLS 握手', http: 'HTTP 响应' };
const contentLabels = {
  matched: '符合预期', mismatch: '内容不匹配', unchecked: '未检查',
  not_configured: '未配置', incomplete: '读取不完整',
};
const websiteClock = new Intl.DateTimeFormat('zh-CN', { dateStyle:'medium', timeStyle:'medium', timeZone:'Asia/Shanghai' });
const websiteStates = { pass: '符合预期', fail: '检测异常', unknown: '结果不完整' };
const websiteTones = { pass: 'success', fail: 'high', unknown: 'missing' };
const certificateLabels = { valid: '证书有效', due: '证书即将到期', urgent: '证书临期紧急', expired: '证书已到期', unknown: '证书未取得' };
const certificateTones = { valid: 'success', due: 'warn', urgent: 'high', expired: 'high', unknown: 'missing' };
const phaseIcons = { dns: 'domain', tcp: 'route', tls: 'lock', http: 'activity' };

function websiteObservationFields(run) {
  const fields = [['检测状态', websiteStates[run.status]],
    ['探测点', run.probe === 'synthetic' ? '本地演示探针' : '本机探针'],
    ['HTTP 状态', String(run.http_status ?? '未取得')],
    ['内容校验', contentLabels[run.content]],
    ['探测地址', run.address || '未取得']];
  const reason = ({ no_public_ipv4: '未解析到公网 IPv4', certificate_verification_failed: '证书校验失败' })[run.reason];
  if (reason) fields.push(['原因', reason]);
  if (run.expected_status !== undefined) {
    fields.push(['预期 HTTP 状态', String(run.expected_status)],
      ['响应文字校验', run.checks_content ? '已设置' : '未设置']);
  }
  for (const [key, label] of Object.entries(phaseLabels)) {
    const phase = run.phases[key];
    fields.push([label, phase ? phase.ms + ' ms · ' + (phase.ok ? '完成' : '失败') : '未执行']);
  }
  fields.push(['累计首字节', run.ttfb_ms === undefined ? '未取得' : run.ttfb_ms + ' ms'],
    ['累计总耗时', run.total_ms === undefined ? '未取得' : run.total_ms + ' ms'],
    ['线上证书到期', formatTime(run.certificate?.expires_at)],
    ['签发者', run.certificate?.issuer || '未取得'],
    ['TLS 版本', run.certificate?.protocol || '未取得']);
  return fields;
}

function showWebsiteObservation(target, run) {
  showDetails('网站检测', target.url, 'HTTPS GET', websiteObservationFields(run), '', run.observed_at, target.source);
}

let scheduleBusy = false;
let displayedSchedule;

function renderWebsiteSchedule(target) {
  const schedule = target?.schedule;
  $('website-schedule').hidden = !schedule;
  if (!schedule) {
    displayedSchedule = undefined;
    return;
  }
  const input = $('website-interval');
  if (displayedSchedule?.id !== target.id || displayedSchedule?.minutes !== schedule.interval_minutes) {
    input.value = schedule.interval_minutes;
    displayedSchedule = { id: target.id, minutes: schedule.interval_minutes };
  }
  const observed = schedule.passed + schedule.failed;
  const rate = observed ? (100 * schedule.passed / observed).toFixed(1) + '%' : '—';
  const next = schedule.enabled ? formatTime(schedule.next_at) : schedule.since ? '已暂停' : '未启用';
  const fields = [
    ['最近一次检测', formatTime(target.history[0]?.observed_at), 'calendar', 'neutral'],
    ['下一次检测', schedule.running ? '检测中 · 下次 ' + next : next,
      schedule.enabled ? 'activity' : 'pause', schedule.enabled ? 'reminder' : 'neutral'],
    ['可用率', rate, 'activity', !observed ? 'neutral' : schedule.failed ? 'warn' : 'success'],
    ['漏测次数', String(schedule.missed), 'missing', schedule.missed ? 'missing' : 'neutral'],
  ];
  $('website-schedule-stats').replaceChildren(...fields.map(([label, value, symbol, tone]) => {
    const cell = el('div', 'website-phase');
    const title = el('small', 'phase-label');
    const mark = icon(symbol);
    mark.dataset.tone = tone;
    title.append(mark, el('span', '', label));
    cell.append(title, el('strong', '', value));
    return cell;
  }));
  $('website-coverage').textContent = '定时检测成功 ' + schedule.passed + ' / ' + observed + ' 次有效结果' +
    (schedule.since ? ' · 自 ' + formatTime(schedule.since) : '');
  $('website-schedule-save').textContent = schedule.enabled ? '保存间隔' : '启用定时';
  $('website-pause').disabled = scheduleBusy || !schedule.enabled;
}

async function saveWebsiteSchedule(enabled) {
  const id = $('website-target').value;
  const current = websites.find((target) => target.id === id);
  if (!current?.schedule || scheduleBusy) return;
  const minutes = enabled ? $('website-interval').valueAsNumber : current.schedule.interval_minutes;
  const controls = ['website-interval', 'website-schedule-save', 'website-pause', 'website-target',
    'website-run', 'show-mock'].map($);
  scheduleBusy = true;
  for (const control of controls) control.disabled = true;
  try {
    const response = await fetch('/api/website-monitors/' + id + '/schedule', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ interval_minutes: minutes, enabled }),
    });
    if (!response.ok) throw new Error('schedule unavailable');
    const target = await response.json();
    websites = websites.map((row) => row.id === id ? target : row);
    overviewMonitors('web', websites);
    $('website-interval').value = target.schedule.interval_minutes;
    $('website-status').textContent = enabled ? '已保存定时计划。' : '已暂停定时检测。';
  } catch {
    $('website-status').textContent = '无法更新定时计划，请重试。';
  } finally {
    scheduleBusy = false;
    for (const control of controls) control.disabled = false;
    renderWebsite(websites.find((target) => target.id === id));
  }
}

async function refreshWebsiteSchedules() {
  if (document.hidden || scheduleBusy || $('website-run').disabled || !websites.some((row) => row.schedule)) return;
  const previous = websites;
  const showMock = $('show-mock').checked;
  try {
    const response = await fetch(withMode('/api/website-monitors'));
    if (!response.ok) return;
    const { targets } = await response.json();
    if (websites !== previous || scheduleBusy || $('website-run').disabled || $('show-mock').checked !== showMock) return;
    const id = $('website-target').value;
    const before = previous.find((target) => target.id === id);
    const after = targets.find((target) => target.id === id);
    websites = targets;
    overviewMonitors('web', websites);
    if (before?.revision !== after?.revision || JSON.stringify(before?.history) !== JSON.stringify(after?.history)) renderWebsite(after);
    else if (JSON.stringify(before?.schedule) !== JSON.stringify(after?.schedule)) renderWebsiteSchedule(after);
    const recorded = targets.some((target) => target.schedule &&
      JSON.stringify(target.history) !== JSON.stringify(previous.find((row) => row.id === target.id)?.history));
    if (recorded) {
      const work = await fetch(withMode('/api/work-items'));
      if (!work.ok) return;
      const result = await work.json();
      if ($('show-mock').checked !== showMock || websites !== targets) return;
      items = result.items;
      renderWorkItems();
    }
  } catch {
    // The next refresh retries cached reads without starting a probe.
  }
}

function renderProbeTimeline(target) {
  const history = target.history.slice(0, 12).reverse();
  const section = el('section', 'probe-timeline');
  section.setAttribute('aria-labelledby', 'probe-timeline-title');
  const heading = el('div', 'probe-timeline-head');
  const title = el('h3', '', '最近 ' + history.length + ' 次检测');
  title.id = 'probe-timeline-title';
  heading.append(title, el('span', '', '从早到晚'));
  const strip = el('ol', 'probe-strip');
  for (const run of history) {
    const entry = el('li');
    const mark = button('probe-mark');
    const tone = websiteTones[run.status];
    const label = websiteClock.format(new Date(run.observed_at)) + ' UTC+8 · ' + websiteStates[run.status];
    mark.dataset.tone = tone;
    mark.setAttribute('aria-label', label);
    mark.title = label;
    mark.append(icon(stateIcons[tone]));
    mark.addEventListener('click', () => showWebsiteObservation(target, run));
    entry.append(mark);
    strip.append(entry);
  }
  const legend = el('div', 'probe-legend');
  for (const [state, tone] of Object.entries(websiteTones)) {
    legend.append(stateBadge(websiteStates[state], tone));
  }
  section.append(heading, strip, legend);
  return section;
}

function websiteHistoryRow(target, run) {
  const row = button('row');
  const label = el('span');
  const time = el('time', 'website-history-time', websiteClock.format(new Date(run.observed_at)) + ' UTC+8');
  time.dateTime = run.observed_at;
  const notes = [run.trigger === 'scheduled' && '定时', run.http_status && 'HTTP ' + run.http_status,
    run.revision !== undefined && '条件版本 ' + (run.revision + 1), contentLabels[run.content]];
  label.append(time, el('small', '', notes.filter(Boolean).join(' · ')));
  row.append(label, stateBadge(websiteStates[run.status], websiteTones[run.status]), icon('chevron'));
  row.addEventListener('click', () => showWebsiteObservation(target, {
    expected_status:target.expected_status, checks_content:target.checks_content, ...run,
  }));
  return row;
}

function renderWebsite(target) {
  renderWebsiteSchedule(target);
  $('website-scenario-control').hidden = target?.source !== 'synthetic';
  const settings = $('website-conditions');
  settings.hidden = !(websiteRegistrationAllowed && target?.source === 'live');
  if (settings.hidden || $('website-conditions-form').dataset.target !== target?.id) settings.open = false;
  const previous = target?.previous_history || [];
  $('website-previous-history').hidden = !previous.length;
  $('website-previous-rows').replaceChildren(...previous.map((run) => websiteHistoryRow(target, run)));
  if (!target?.history.length) {
    const message = target?.revision ? '新条件下尚无检测记录' : target ? '尚无检测记录' : '还没有已登记的 URL';
    $('website-results').replaceChildren(el('p', 'empty', message));
    return;
  }
  const latest = target.history[0];
  const summary = el('div', 'website-phases');
  const maxPhaseMs = Math.max(1, ...Object.values(latest.phases).map((phase) => phase.ms));
  for (const [key, label] of Object.entries(phaseLabels)) {
    const phase = latest.phases[key];
    const cell = el('div', 'website-phase');
    const title = el('small', 'phase-label');
    title.append(icon(phaseIcons[key]), el('span', '', label));
    const value = el('strong', '', phase ? phase.ms + ' ms' + (phase.ok ? '' : ' · 失败') : '未执行');
    if (phase && !phase.ok) value.dataset.tone = 'high';
    const track = el('span', 'phase-track');
    track.setAttribute('aria-hidden', 'true');
    if (phase) {
      const bar = el('span');
      bar.dataset.tone = phase.ok ? 'reminder' : 'high';
      bar.style.inlineSize = (100 * phase.ms / maxPhaseMs) + '%';
      track.append(bar);
    }
    cell.append(title, value, track);
    summary.append(cell);
  }
  const certificate = el('div', 'website-certificate certificate-detail');
  const online = target.certificate_status;
  const state = online?.status || 'unknown';
  certificate.append(stateBadge(certificateLabels[state], certificateTones[state]));
  if (online?.expires_at) {
    certificate.append(el('span', '', '线上证书到期 ' + formatTime(online.expires_at) +
      ' · 检测时剩余 ' + online.days_remaining + ' 天'));
  }
  const phaseSection = el('section', 'probe-phases');
  phaseSection.setAttribute('aria-labelledby', 'probe-phases-title');
  const phaseHeading = el('div', 'probe-timeline-head');
  const phaseTitle = el('h3', '', '最近一次分段耗时');
  phaseTitle.id = 'probe-phases-title';
  phaseHeading.append(phaseTitle);
  if (Number.isFinite(latest.total_ms)) phaseHeading.append(el('span', '', '总计 ' + latest.total_ms + ' ms'));
  phaseSection.append(phaseHeading, summary, certificate);
  const historyTitle = el('h3', 'website-history-title', '检测历史');
  const rows = target.history.map((run) => websiteHistoryRow(target, run));
  $('website-results').replaceChildren(phaseSection, renderProbeTimeline(target), historyTitle, ...rows);
}

async function loadWebsites() {
  try {
    const response = await fetch(withMode('/api/website-monitors'));
    if (!response.ok) throw new Error('monitor list unavailable');
    const selected = $('website-target').value;
    const snapshot = await response.json();
    websites = snapshot.targets;
    websiteRegistrationAllowed = snapshot.editable === true;
    $('website-registration').hidden = !websiteRegistrationAllowed;
    overviewMonitors('web', websites);
    $('website-controls').hidden = !websites.length;
    $('website-target').replaceChildren(...websites.map((target) => {
      const option = el('option', '', target.url + (target.source === 'synthetic' ? ' · 模拟' : ''));
      option.value = target.id;
      return option;
    }));
    if (websites.some((target) => target.id === selected)) $('website-target').value = selected;
    $('website-status').textContent = '';
    renderWebsite(websites.find((target) => target.id === $('website-target').value));
    if (resourceRoute().section === 'websites') applyResourceRoute(document.activeElement === document.body);
    return true;
  } catch {
    overviewCard('web', '—', '读取失败', 'missing');
    $('website-status').textContent = '无法读取网站监控目标。';
    return false;
  }
}

async function registerWebsite(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('button[type=submit]');
  if (submit.disabled) return;
  const fields = new FormData(form);
  const status = $('website-registration-status');
  submit.disabled = true;
  try {
    const response = await fetch('/api/website-monitors', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
        url:fields.get('url'), expected_status:Number(fields.get('expected_status')), contains:fields.get('contains') || null,
      }),
    });
    if (response.status === 400) {
      status.textContent = '请核对 HTTPS 地址、状态码和响应文字。地址支持路径，响应文字最多 512 字节。';
      return;
    }
    if (!response.ok) throw new Error('Registration request failed');
    const result = await response.json();
    if (result.status === 'limit') {
      status.textContent = '已达到 100 个网站监控的上限。';
      return;
    }
    form.reset();
    $('website-registration').open = false;
    if (!await loadWebsites()) {
      status.textContent = '网站已登记。刷新页面后可以查看。';
      return;
    }
    const hash = '#websites?' + new URLSearchParams({target:result.id});
    history.replaceState(null, '', hash);
    updateNavigation();
    applyResourceRoute();
    status.textContent = result.status === 'existing' ? '已打开原网站监控，保留原有设置。' : '网站已登记，可以开始检测。';
    $('website-run').focus();
  } catch {
    status.textContent = '暂时无法保存网站，请稍后重试。';
  } finally {
    submit.disabled = false;
  }
}

function updateWebsiteContentField() {
  const replacing = $('website-content-action').value === 'replace';
  $('website-conditions-text').hidden = !replacing;
  $('website-expected-text').disabled = !replacing;
  $('website-expected-text').required = replacing;
}

async function saveWebsiteConditions(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (scheduleBusy || $('website-run').disabled || form.dataset.target !== $('website-target').value) return;
  const fields = new FormData(form);
  const submit = form.querySelector('button[type=submit]');
  const controls = [submit, ...['website-target', 'website-run', 'website-schedule-save', 'website-pause', 'show-mock'].map($)];
  const status = $('website-conditions-status');
  scheduleBusy = true;
  for (const control of controls) control.disabled = true;
  try {
    const response = await fetch('/api/website-monitors/' + form.dataset.target + '/conditions', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
        revision:Number(form.dataset.revision), expected_status:Number(fields.get('expected_status')),
        content_action:fields.get('content_action'), contains:fields.get('contains'),
      }),
    });
    if (response.status === 409) {
      await loadWebsites();
      status.textContent = '检测条件已在其他页面更新。请重新打开编辑表单后再保存。';
      return;
    }
    if (response.status === 400) {
      status.textContent = '请核对状态码和响应文字。响应文字最多 512 字节。';
      return;
    }
    if (!response.ok) throw new Error('Website conditions unavailable');
    const result = await response.json();
    $('website-conditions').open = false;
    if (result.status === 'updated') {
      const loaded = await loadWebsites();
      status.textContent = loaded ? '检测条件已更新。可以立即检测查看新结果。' : '检测条件已更新。刷新页面后可查看。';
      await runInspection(false, true);
    } else {
      status.textContent = '检测条件没有变化。';
    }
    $('website-conditions-label').focus();
  } catch {
    status.textContent = '无法保存检测条件，请稍后重试。';
  } finally {
    scheduleBusy = false;
    for (const control of controls) control.disabled = false;
    renderWebsiteSchedule(websites.find((target) => target.id === $('website-target').value));
  }
}

async function runWebsite() {
  const id = $('website-target').value;
  if (!id) return;
  const controls = [$('website-run'), $('website-target'), $('website-scenario'), $('show-mock')];
  for (const control of controls) control.disabled = true;
  $('website-results').ariaBusy = 'true';
  for (const key of ['website-schedule-save', 'website-pause']) $(key).disabled = true;
  try {
    const response = await fetch('/api/website-monitors/' + id + '/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scenario: $('website-scenario').value, show_mock: $('show-mock').checked }),
    });
    if (!response.ok) throw new Error('monitor failed');
    const target = await response.json();
    websites = websites.map((row) => row.id === id ? target : row);
    overviewMonitors('web', websites);
    renderWebsite(target);
    $('website-status').textContent = websiteStates[target.history[0].status] + ' · ' +
      certificateLabels[target.certificate_status.status];
    const work = await fetch(withMode('/api/work-items'));
    if (!work.ok) throw new Error('work items unavailable');
    items = (await work.json()).items;
    renderWorkItems();
  } catch {
    $('website-status').textContent = '无法完成检测或刷新待办，请重试。';
  } finally {
    $('website-results').ariaBusy = 'false';
    $('website-schedule-save').disabled = false;
    renderWebsiteSchedule(websites.find((target) => target.id === id));
    for (const control of controls) control.disabled = false;
  }
}

function withMode(url) {
  return url + '?mock=' + ($('show-mock').checked ? '1' : '0');
}

function updateMode(development) {
  $('mock-control').hidden = !development;
  $('scenario-control').hidden = !development || !$('show-mock').checked;
  $('mode-name').textContent = development ? '开发模式' : '真实连接';
  document.querySelector('.mobile-demo').lastChild.textContent = development ? '开发模式' : '本地连接';
  if (!development) $('show-mock').checked = false;
}

let inspectionHistoryBefore = null;
let inspectionHistoryRequest;

function renderInspectionRun(run) {
  const details = el('details');
  const title = el('summary');
  const time = el('time', '', formatTime(run.observed_at));
  const trigger = {manual:'手动', scheduled:'定时', legacy:'旧版记录'}[run.trigger];
  time.dateTime = run.observed_at;
  title.append(time, ' · ' + trigger + (run.selected_accounts ? ' · 指定账号' : '') +
    ' · 查完 ' + run.coverage.complete + '/' + run.expected_accounts);

  const coverage = Object.entries(run.coverage).map(([key, count]) => statusLabels[key] + ' ' + count);
  const findings = Object.entries(run.counts).map(([key, count]) =>
    (key === 'billing' ? '费用增长' : key === 'balance' ? '余额预警' : categories[key]?.label || key) + ' ' + count);
  const accounts = el('ul', 'inspection-history-accounts');
  for (const account of run.accounts) {
    const line = el('li');
    const name = el('span', '', account.label);
    name.append(el('small', '', account.provider + ' · ' + account.id));
    line.append(name, stateBadge(statusLabels[account.status] || '未取得', coverageTones[account.status] || 'missing'));
    accounts.append(line);
  }
  details.append(title, el('p', 'inspection-plan-copy', coverage.join(' · ')),
    el('p', 'inspection-plan-copy', findings.join(' · ')), accounts);
  return details;
}

async function loadInspectionHistory(append = false) {
  const rows = $('inspection-history-rows');
  const select = $('inspection-history-source');
  const more = $('inspection-history-more');
  const status = $('inspection-history-status');
  const showMock = !$('mock-control').hidden && $('show-mock').checked;
  $('inspection-history-source-control').hidden = !showMock;
  if (!showMock) select.value = 'live';
  const query = new URLSearchParams({source:select.value, mock:showMock ? '1' : '0'});
  if (append && inspectionHistoryBefore) query.set('before', inspectionHistoryBefore);
  if (!append) {
    rows.replaceChildren();
    more.hidden = true;
    inspectionHistoryBefore = null;
  }
  inspectionHistoryRequest?.abort();
  const request = new AbortController();
  inspectionHistoryRequest = request;
  rows.setAttribute('aria-busy', 'true');
  more.disabled = true;
  status.textContent = '';
  try {
    const response = await fetch('/api/inspection/history?' + query, {signal:request.signal});
    if (!response.ok) throw new Error('Inspection history unavailable');
    const {runs, before} = await response.json();
    if (request.signal.aborted) return;
    const group = runs.map(renderInspectionRun);
    rows.append(...group);
    inspectionHistoryBefore = before;
    more.hidden = before === null;
    status.textContent = rows.childElementCount ? '已显示 ' + rows.childElementCount + ' 轮巡检' : '暂无巡检历史。';
    if (append && group.length) group[0].querySelector('summary').focus();
  } catch {
    if (!request.signal.aborted) status.textContent = '巡检历史读取失败，请重试。';
  } finally {
    if (!request.signal.aborted) {
      rows.setAttribute('aria-busy', 'false');
      more.disabled = false;
    }
  }
}

let inspectionPlan;
let inspectionPlanBusy = false;

function editInspectionScope() {
  const all = $('inspection-plan-all-accounts').checked;
  $('inspection-plan-account-choices').hidden = all;
  $('inspection-plan-run-selection').hidden = all;
}

function selectedInspectionAccounts() {
  if ($('inspection-plan-all-accounts').checked) return null;
  return [...$('inspection-plan-account-choices').querySelectorAll('input:checked')].map(control => control.value);
}

function renderInspectionPlanAccounts(plan) {
  const all = $('inspection-plan-all-accounts');
  const container = $('inspection-plan-account-choices');
  all.checked = plan.account_ids === null;
  editInspectionScope();
  const accounts = new Map(plan.accounts.map(account => [account.id, account.label + ' · ' + account.id]));
  for (const id of plan.account_ids ?? []) {
    if (!accounts.has(id)) accounts.set(id, id + ' · 当前不可用');
  }
  const options = [...accounts].map(([id, name], index) => {
    const control = el('input');
    control.type = 'checkbox';
    control.value = id;
    control.id = 'inspection-account-' + index;
    control.checked = plan.account_ids?.includes(id) ?? false;
    const label = el('label');
    label.htmlFor = control.id;
    label.append(control, ' ' + name);
    return label;
  });
  container.replaceChildren(...(options.length ? options : [el('p', 'empty', '暂无可选账号')]));
}

function renderInspectionPlan(plan, resetInterval = false) {
  inspectionPlan = plan;
  if (resetInterval) $('inspection-plan-interval').value = plan.interval_minutes;
  $('inspection-plan-state').textContent = plan.running ? '巡检中' : plan.enabled ? '已启用' : '未启用';
  $('inspection-plan-times').textContent = '最近巡检 ' + formatTime(plan.last_at) + ' · 下次计划 ' +
    formatTime(plan.next_at) + ' · 漏巡 ' + plan.missed + ' 次 · ' +
    (plan.account_ids ? '选定 ' + plan.account_ids.length + ' 个账号' : '全部已启用账号');
  $('inspection-plan-save').textContent = plan.enabled ? '保存间隔' : '启用定时';
  $('inspection-plan-save').disabled = inspectionPlanBusy;
  $('inspection-plan-pause').disabled = inspectionPlanBusy || !plan.enabled;
  $('inspection-plan-accounts').disabled = inspectionPlanBusy;
  $('inspection-plan-run-selection').disabled = inspectionPlanBusy;
}

async function refreshInspectionPlan({force = false, reload = true, includeAccounts = force} = {}) {
  const previous = inspectionPlan;
  if (inspectionPlanBusy || (!force && (document.hidden || !(previous?.enabled || previous?.running)))) return;
  try {
    const response = await fetch('/api/inspection/plan' + (includeAccounts ? '?accounts=1' : ''));
    if (!response.ok) throw new Error('Inspection plan unavailable');
    const plan = await response.json();
    if (inspectionPlanBusy || inspectionPlan !== previous) return;
    renderInspectionPlan(plan, !previous);
    if (plan.accounts) renderInspectionPlanAccounts(plan);
    if (reload && previous && plan.last_at !== previous.last_at && !$('run').disabled) {
      await runInspection(false, true);
      await loadAssets();
    }
  } catch {
    if (!previous) $('inspection-plan-feedback').textContent = '巡检计划读取失败，请重新展开重试。';
  }
}

async function saveInspectionPlan(enabled) {
  if (!inspectionPlan || inspectionPlanBusy) return;
  const interval = $('inspection-plan-interval');
  const feedback = $('inspection-plan-feedback');
  const body = {enabled, interval_minutes:enabled ? interval.valueAsNumber : inspectionPlan.interval_minutes};
  if (enabled) {
    body.account_ids = selectedInspectionAccounts();
    if (body.account_ids?.length === 0) {
      feedback.textContent = '请选择至少一个巡检账号。';
      return;
    }
  }
  inspectionPlanBusy = true;
  renderInspectionPlan(inspectionPlan);
  interval.disabled = true;
  feedback.textContent = '';
  try {
    const response = await fetch('/api/inspection/plan', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body),
    });
    if (response.status === 409) {
      feedback.textContent = body.account_ids ?
        '所选范围内有账号当前不可巡检，请核对服务接入或重新选择。' :
        '请先在「服务接入」启用支持巡检的连接。';
      return;
    }
    if (!response.ok) throw new Error('Inspection plan unavailable');
    const plan = await response.json();
    renderInspectionPlan(plan, true);
    renderInspectionPlanAccounts(plan);
    feedback.textContent = enabled ? '巡检计划已保存。' : '定时巡检已暂停。';
  } catch {
    feedback.textContent = '巡检计划保存失败，请重试。';
  } finally {
    inspectionPlanBusy = false;
    interval.disabled = false;
    renderInspectionPlan(inspectionPlan);
  }
}

async function runSelectedInspection() {
  const mainRun = $('run');
  if (!inspectionPlan || inspectionPlanBusy || mainRun.disabled) return;
  const accountIds = selectedInspectionAccounts();
  const feedback = $('inspection-plan-feedback');
  if (!accountIds?.length) {
    feedback.textContent = '请选择至少一个巡检账号。';
    return;
  }
  const runButton = $('inspection-plan-run-selection');
  const interval = $('inspection-plan-interval');
  inspectionPlanBusy = true;
  renderInspectionPlan(inspectionPlan);
  interval.disabled = mainRun.disabled = true;
  runButton.textContent = '巡检中…';
  feedback.textContent = '';
  try {
    const response = await fetch('/api/inspection/summary', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({account_ids:accountIds, show_mock:false}),
    });
    if (response.status === 409) {
      feedback.textContent = '所选账号已变化，请重新展开计划并选择。';
      return;
    }
    if (!response.ok) throw new Error('Inspection unavailable');
    const result = await response.json();
    await runInspection(false, true);
    await loadAssets();
    const missing = result.expected_accounts - result.coverage.complete;
    feedback.textContent = result.recorded ? '已巡检 ' + result.expected_accounts + ' 个所选账号' +
      (missing ? '，其中 ' + missing + ' 个未查全。' : '，全部查完。') : '工作台沿用较新的巡检结果。';
  } catch {
    feedback.textContent = '未能确认本次巡检结果，请先核对巡检历史。';
  } finally {
    inspectionPlanBusy = false;
    interval.disabled = mainRun.disabled = false;
    runButton.textContent = '立即巡检所选账号';
    renderInspectionPlan(inspectionPlan);
    await refreshInspectionPlan({force:true, reload:false, includeAccounts:false});
  }
}

async function runInspection(initial = false, readOnly = false) {
  $('run').disabled = true;
  $('scenario').disabled = true;
  $('show-mock').disabled = true;
  $('metrics').ariaBusy = 'true';
  $('run-status').textContent = initial || readOnly ? '正在读取巡检记录' : '巡检中';
  $('error').hidden = true;
  try {
    if (initial || readOnly) {
      const response = await fetch(withMode('/api/work-items'));
      if (!response.ok) throw new Error('snapshot request failed');
      const snapshot = await response.json();
      updateMode(snapshot.development);
      if (snapshot.report?.expected_accounts) {
        if (initial && snapshot.report.scenario !== 'live') $('scenario').value = snapshot.report.scenario;
        renderSnapshot(snapshot);
        $('run-status').textContent = '已载入最近巡检';
      } else {
        renderSnapshot(snapshot);
        $('run-status').textContent = '待首次巡检';
      }
      return;
    }
    const response = await fetch('/api/inspection', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scenario: $('scenario').value, show_mock: $('show-mock').checked }),
    });
    if (!response.ok) throw new Error('inspection request failed');
    const snapshot = await response.json();
    updateMode(snapshot.development);
    renderSnapshot(snapshot);
    $('run-status').textContent = !snapshot.report?.expected_accounts ? '尚未配置可巡检账号' :
      snapshot.recorded ? '巡检完成' : '已保留最近巡检';
    await loadAssets();
  } catch {
    $('run-status').textContent = report ? '更新失败 · 显示上轮结果' : '巡检失败';
    $('error').textContent = '巡检结果读取失败。请重新巡检。';
    $('error').hidden = false;
  } finally {
    $('run').disabled = false;
    $('scenario').disabled = false;
    $('show-mock').disabled = false;
    $('metrics').ariaBusy = 'false';
  }
}

function updateNavigation() {
  const activeHash = location.hash.split('?')[0] || '#main';
  for (const link of document.querySelectorAll('.sidebar nav a')) {
    const current = link.hash === activeHash;
    link.classList.toggle('current', current);
    if (current) link.setAttribute('aria-current', 'location');
    else link.removeAttribute('aria-current');
  }
}

window.addEventListener('hashchange', () => {
  updateNavigation();
  focusOnboardingAccount();
  applyResourceRoute(true);
  applyAssetRoute();
  applyWorkItemRoute();
});
updateNavigation();
focusOnboardingAccount();
for (const node of document.querySelectorAll('[data-icon]')) drawIcon(node, node.dataset.icon);
$('run').addEventListener('click', async () => {
  await runInspection();
  await refreshInspectionPlan({force:true, reload:false});
});
$('inspection-plan-form').addEventListener('submit', event => {
  event.preventDefault();
  saveInspectionPlan(true);
});
$('inspection-plan-pause').addEventListener('click', () => saveInspectionPlan(false));
$('inspection-plan-all-accounts').addEventListener('change', editInspectionScope);
$('inspection-plan-run-selection').addEventListener('click', runSelectedInspection);
document.querySelector('.inspection-plan').addEventListener('toggle', event => {
  if (event.currentTarget.open) refreshInspectionPlan({force:true});
});
refreshInspectionPlan({force:true});
setInterval(refreshInspectionPlan, 5000);
$('inspection-history').addEventListener('toggle', event => {
  if (event.currentTarget.open) loadInspectionHistory();
});
$('inspection-history-source').addEventListener('change', () => loadInspectionHistory());
$('inspection-history-refresh').addEventListener('click', () => loadInspectionHistory());
$('inspection-history-more').addEventListener('click', () => loadInspectionHistory(true));
async function refreshAfterConnectionChange() {
  await runInspection(false, true);
  await refreshInspectionPlan({force:true, reload:false});
}
window.addEventListener('stackorion:onboarding-saved', refreshAfterConnectionChange);
window.addEventListener('stackorion:connection-state-changed', refreshAfterConnectionChange);
$('show-mock').checked = localStorage.getItem('stackorion.showMock') !== '0';
$('show-mock').addEventListener('change', async () => {
  localStorage.setItem('stackorion.showMock', $('show-mock').checked ? '1' : '0');
  if ($('inspection-history').open) loadInspectionHistory();
  // Refresh the display only. Changing visibility never starts an upstream query.
  await runInspection(false, true);
  if ($('show-mock').checked && !report?.simulated_at) {
    $('run-status').textContent = '当前无可显示的模拟结果';
  }
  await Promise.all([loadAssets(), loadWebsites(), loadHosts()]);
});
$('website-target').addEventListener('change', () => {
  rememberMonitorSelection('websites', $('website-target'));
  $('website-status').textContent = '';
  renderWebsite(websites.find((target) => target.id === $('website-target').value));
});
$('website-run').addEventListener('click', runWebsite);
$('website-registration-form').addEventListener('submit', registerWebsite);
$('website-conditions-form').addEventListener('submit', saveWebsiteConditions);
$('website-content-action').addEventListener('change', updateWebsiteContentField);
$('website-conditions').addEventListener('toggle', (event) => {
  if (!event.currentTarget.open) return;
  const target = websites.find((row) => row.id === $('website-target').value);
  const form = $('website-conditions-form');
  if (!target) return;
  form.reset();
  form.dataset.target = target.id;
  form.dataset.revision = target.revision;
  $('website-expected-status').value = target.expected_status;
  $('website-content-action').options[0].text = target.checks_content ? '保留当前校验' : '保持关闭';
  $('website-conditions-status').textContent = '';
  updateWebsiteContentField();
});
runInspection(true).then(() => Promise.all([loadAssets(), loadWebsites(), loadHosts()])).then(() => {
  if (!$('details').open) applyWorkItemRoute();
});
$('website-schedule-form').addEventListener('submit', (event) => {
  event.preventDefault();
  saveWebsiteSchedule(true);
});
$('website-pause').addEventListener('click', () => saveWebsiteSchedule(false));
setInterval(refreshWebsiteSchedules, 5000);

const hostSources = { qvm:'七牛 QVM · 内网', host:'主机只读采集', synthetic:'模拟', 'las-api':'七牛 LAS 只读接口', 'linux-ssh':'Linux SSH · 只读' };
const hostStates = { available:'云端可用', running:'运行中', stopped:'已停止',
  starting:'启动中', stopping:'停止中', unavailable:'不可用' };
let hostRows = [];
let hostBusy = false;
let selectedHostInterval;
let hostMetric = 0;
let metricSnapshot;
let trendKey;
let trendData;
let hostPlot;
let historyRequest;

function currentHost() {
  return hostRows.find((row) => row.id === $('host-target').value);
}

function hostProduct(target) {
  return target.source === 'synthetic' ? '模拟' :
    ((target.provider === 'qiniu' ? '七牛' : target.provider) + ' ' + target.product.toUpperCase());
}

function renderHostMetrics(target) {
  const last = target.history[0];
  const grid = $('host-metrics');
  const signature = target.id + ':' + last.observed_at;
  if (metricSnapshot === signature) return;
  metricSnapshot = signature;
  grid.replaceChildren(...metricGroupsFor(target.history).map((group, index) => {
    const card = button('host-metric');
    card.dataset.metric = index;
    card.setAttribute('aria-pressed', String(index === hostMetric));
    const title = el('span', 'host-metric-label');
    title.append(icon(group.icon), el('span', '', group.name));
    const points = group.series.map(([key]) => last.metrics[key]);
    const available = points.find(Boolean);
    const value = el('strong', 'host-metric-value', group.series.map(([, label], i) =>
      (group.series.length > 1 ? label + ' ' : '') + formattedSample(points[i], group.unit)
    ).join('  /  '));
    if (!available) value.dataset.tone = 'missing';
    const previous = !available && target.history.slice(1)
      .flatMap((run) => group.series.map(([key]) => run.metrics[key])).find(Boolean);
    const source = available ? [hostSources[available.source] || available.source, formatTime(available.observed_at)].join(' · ')
      : previous ? '上次取得 ' + formatTime(previous.observed_at) : '暂无有效采集';
    card.append(title, value, el('small', '', source));
    card.addEventListener('click', () => {
      hostMetric = index;
      for (const tile of grid.children) tile.setAttribute('aria-pressed', String(tile === card));
      drawHostTrend();
    });
    return card;
  }));
}

function renderHostHistory() {
  const details = $('host-history');
  const scroller = $('host-history-scroll');
  const history = trendData?.history || [];
  details.hidden = !history.length;
  if (!details.open || details.hidden) {
    scroller.replaceChildren();
    return;
  }
  const table = el('table');
  table.append(el('caption', 'sr-only', '服务器采集指标与时间'));
  const head = el('thead');
  const headings = el('tr');
  const groups = metricGroupsFor([...history, ...(currentHost()?.history || [])]);
  const network = groups.at(-1);
  const labels = ['采集时间', 'CPU %', '内存 %', '根分区 %', ...network.series.map(([, label]) => label + network.unit)];
  if (history.some((run) => run.resource_state)) labels.splice(1, 0, '实例状态');
  for (const label of labels) {
    const cell = el('th', '', label);
    cell.scope = 'col';
    headings.append(cell);
  }
  head.append(headings);
  const body = el('tbody');
  for (const run of history) {
    const row = el('tr');
    const stamp = websiteClock.format(new Date(run.observed_at)) + ' UTC+8';
    row.append(el('td', '', stamp + (run.trigger === 'scheduled' ? ' · 定时' : ' · 立即')));
    if (labels[1] === '实例状态') row.append(el('td', '', hostStates[run.resource_state] || '未取得'));
    for (const group of groups) {
      for (const [key] of group.series) row.append(el('td', '', formattedSample(run.metrics[key], '')));
    }
    body.append(row);
  }
  table.append(head, body);
  scroller.replaceChildren(table);
}

function drawHostTrend() {
  hostPlot?.destroy();
  hostPlot = null;
  const group = metricGroupsFor([...(trendData?.history || []), ...(currentHost()?.history || [])])[hostMetric];
  const zoomY = Boolean(group.max && $('host-axis').value === 'local');
  $('host-axis-control').hidden = !group.max;
  $('host-trend-title').textContent = group.name + '趋势';
  const status = $('host-chart-status');
  const projection = $('host-disk-projection');
  const estimate = trendData?.disk_projection;
  projection.hidden = hostMetric !== 2 || !estimate;
  if (!projection.hidden) {
    const descriptions = {
      insufficient_samples: '样本不足，需要至少 5 次有效采集并覆盖 4 小时',
      latest_missing: '最近一次采集未取得磁盘使用率',
      stale: '最近一次有效磁盘样本已过时',
      threshold_reached: '最近采集已达到 ' + estimate.threshold + '%',
      interrupted: '连续样本不足，期间有漏采或采集间隔过长',
      source_changed: '采集来源发生切换，同来源样本不足',
      truncated: '历史已截断，连续样本跨度不足',
      no_growth: '当前样本未显示持续增长',
      unstable: '磁盘使用率波动较大',
      outside_window: '预计达到阈值的时间超出样本跨度',
      recheck: '推算时点已过，请重新采集确认',
    };
    const detail = estimate.status === 'projected'
      ? '按最近样本趋势，预计 ' + formatTime(estimate.projected_at) + ' 达到 ' + estimate.threshold +
        '% · 每小时增加 ' + estimate.rate_per_hour + ' 个百分点'
      : descriptions[estimate.status];
    projection.textContent = '磁盘预估 · ' + detail + ' · ' + estimate.samples + ' 次同源采集 / ' +
      estimate.span_hours + ' 小时';
  }
  const window = $('host-window').selectedOptions[0].textContent;
  if (!trendData) {
    $('host-trend-summary').textContent = window + ' · UTC+8';
    status.textContent = '正在读取趋势';
    status.hidden = false;
    return;
  }
  const count = trendData.history.length;
  const valid = new Set(trendData.history.filter((run) =>
    group.series.some(([key]) => run.metrics[key])).map((run) => run.observed_at)).size;
  const metadata = [window, 'UTC+8', valid + ' / ' + count + ' 次有数据'];
  if (valid > 0 && valid < 5) metadata.push('样本较少');
  if (trendData.truncated) metadata.push('仅显示最新记录');
  if (zoomY) metadata.push('局部刻度');
  if (group.series.length === 2) {
    metadata.push((group.unit === ' kbit/s' ? 'kbit/s' : '每采样周期 kbit') + '，发送为虚线');
  }
  $('host-trend-summary').textContent = metadata.join(' · ');
  try {
    hostPlot = mountHostTrend($('host-chart'), group, trendData.history, $('host-zoom-reset'), zoomY);
    status.textContent = count ? '这段时间未取得有效指标' : '所选时间范围内没有采集记录';
    status.hidden = Boolean(hostPlot);
  } catch {
    status.textContent = '趋势图加载失败，可查看采集明细';
    status.hidden = false;
  }
  if (trendData.error) {
    status.textContent = '无法读取趋势，可查看最近采集明细';
    status.hidden = false;
  }
}

async function requestHostHistory(target) {
  const hours = $('host-window').value;
  const signature = [target?.id, target?.history[0]?.observed_at, hours].join(':');
  if (trendKey === signature) return;
  trendKey = signature;
  historyRequest?.abort();
  trendData = null;
  renderHostHistory();
  drawHostTrend();
  if (!target?.history.length) return;
  const controller = new AbortController();
  historyRequest = controller;
  try {
    const url = withMode('/api/host-monitors/' + target.id + '/history') + '&hours=' + hours;
    const response = await fetch(url, {signal:controller.signal});
    if (!response.ok) throw new Error('host history unavailable');
    const data = await response.json();
    if (controller.signal.aborted) return;
    trendData = data;
  } catch {
    if (controller.signal.aborted) return;
    trendData = {history:target.history, error:true};
  }
  renderHostHistory();
  drawHostTrend();
}

function renderHost(target) {
  $('host-schedule').hidden = !target;
  $('host-schedule-form').hidden = !target;
  $('host-type').hidden = !target;
  $('host-scenario-control').hidden = target?.source !== 'synthetic';
  const last = target?.history[0];
  $('host-empty').hidden = Boolean(last);
  $('host-metrics').hidden = !last;
  $('host-trend-frame').hidden = !last;
  if (!target) {
    selectedHostInterval = undefined;
    metricSnapshot = undefined;
    $('host-empty').textContent = '还没有已登记的服务器监控目标';
    requestHostHistory(null);
    return;
  }
  $('host-type').textContent = hostProduct(target);
  const schedule = target.schedule;
  const next = schedule.enabled ? formatTime(schedule.next_at) : schedule.since ? '已暂停' : '未启用';
  const times = [
    ['最近采集', last ? formatTime(last.observed_at) : '—'],
    ['下一次采集', schedule.running ? '正在采集 · ' + next : next],
    ['定时漏采', schedule.missed + ' 次', schedule.missed ? 'missing' : null],
  ];
  if (last?.resource_state) times.push(['实例状态', hostStates[last.resource_state],
    ['running','available'].includes(last.resource_state) ? 'success' : 'neutral',
    [hostSources[last.state_source] || last.state_source, formatTime(last.state_observed_at)].join(' · ')]);
  $('host-timing').replaceChildren(...times.map(([label, value, tone, provenance]) => {
    const pair = el('div');
    const detail = el('dd', '', value);
    if (tone) detail.dataset.tone = tone;
    pair.append(el('dt', '', label), detail);
    if (provenance) detail.append(el('small', '', provenance));
    return pair;
  }));
  if (selectedHostInterval?.id !== target.id || selectedHostInterval?.value !== schedule.interval_minutes) {
    $('host-interval').value = schedule.interval_minutes;
    selectedHostInterval = {id:target.id, value:schedule.interval_minutes};
  }
  $('host-schedule-save').textContent = schedule.enabled ? '保存间隔' : '启用定时';
  $('host-pause').disabled = hostBusy || !schedule.enabled;
  if (!last) {
    metricSnapshot = undefined;
    $('host-empty').textContent = '暂无采集记录，可立即采集或启用定时';
  } else {
    renderHostMetrics(target);
  }
  requestHostHistory(target);
}

async function refreshWorkItems() {
  const mode = withMode('/api/work-items');
  const response = await fetch(mode);
  if (!response.ok) throw new Error('work items unavailable');
  const snapshot = await response.json();
  if (mode !== withMode('/api/work-items')) return;
  items = snapshot.items;
  renderWorkItems();
}

setInterval(() => {
  if (!document.hidden && items.some(item => item.exception?.active &&
      Date.parse(item.exception.review_on + 'T00:00:00+08:00') <= Date.now())) {
    refreshWorkItems().catch(() => {});
  }
}, 60000);

async function loadHosts(refresh = false) {
  if (hostBusy || (refresh && (document.hidden || !hostRows.some((target) => target.schedule.enabled || target.schedule.running)))) return;
  const previous = hostRows;
  const mode = withMode('/api/host-monitors');
  try {
    const response = await fetch(mode);
    if (!response.ok) throw new Error('hosts unavailable');
    const { targets } = await response.json();
    if (hostBusy || hostRows !== previous || mode !== withMode('/api/host-monitors')) return;
    const before = currentHost();
    hostRows = targets;
    overviewMonitors('host', hostRows);
    if (!refresh) {
      const select = $('host-target');
      const id = select.value;
      select.replaceChildren(...targets.map((target) => {
        const option = el('option', '', target.name + ' · ' + hostProduct(target));
        option.value = target.id;
        return option;
      }));
      if (targets.some((target) => target.id === id)) select.value = id;
      $('host-controls').hidden = !targets.length;
    }
    const after = currentHost();
    if (!refresh || JSON.stringify(before) !== JSON.stringify(after)) renderHost(after);
    if (!refresh && resourceRoute().section === 'servers') applyResourceRoute(document.activeElement === document.body);
    if (refresh && targets.some((target) => target.history[0]?.observed_at !== previous.find((entry) => entry.id === target.id)?.history[0]?.observed_at)) {
      await refreshWorkItems();
    }
  } catch {
    if (!refresh) {
      overviewCard('host', '—', '读取失败', 'missing');
      $('host-status').textContent = '无法读取服务器监控目标。';
    }
  }
}

async function submitHost(action, enabled) {
  const target = currentHost();
  if (!target || hostBusy) return;
  hostBusy = true;
  const controls = ['host-target','host-scenario','host-run','host-interval','host-schedule-save','host-pause','show-mock'].map($);
  for (const control of controls) control.disabled = true;
  const status = $('host-status');
  status.textContent = action === 'run' ? '正在采集' : '正在保存';
  const showMock = $('show-mock').checked;
  try {
    const data = action === 'run'
      ? {show_mock:showMock, scenario:$('host-scenario').value}
      : {show_mock:showMock, enabled, interval_minutes:enabled ? $('host-interval').valueAsNumber : target.schedule.interval_minutes};
    const response = await fetch('/api/host-monitors/' + target.id + '/' + action, {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data),
    });
    if (!response.ok) throw new Error('host monitor request failed');
    const updated = await response.json();
    hostRows = hostRows.map((row) => row.id === updated.id ? updated : row);
    overviewMonitors('host', hostRows);
    if (action === 'run') {
      status.textContent = updated.history[0].status === 'pass' ? '采集完成' : updated.history[0].resource_state ? '实例状态已读回，运行指标未取得' : '部分或全部指标漏采';
      await refreshWorkItems();
    } else {
      status.textContent = enabled ? '已保存定时计划' : '已暂停定时采集';
    }
  } catch {
    status.textContent = '无法完成操作，请重试。';
  } finally {
    hostBusy = false;
    for (const control of controls) control.disabled = false;
    renderHost(currentHost());
  }
}

function showHostRegistrationIdentity() {
  const select = $('host-registration-source');
  $('host-registration-identity').textContent = select.selectedOptions[0]?.dataset.identity || '';
}

async function loadHostSources() {
  const request = Symbol();
  hostSourcesRequest = request;
  const asset = hostRegistrationAsset;
  const form = $('host-registration-form');
  const select = $('host-registration-source');
  const status = $('host-registration-status');
  form.hidden = true;
  status.textContent = '正在读取已登记的采集入口。';
  try {
    const response = await fetch('/api/host-monitors/available');
    if (!response.ok) throw new Error('Sources unavailable');
    const result = await response.json();
    if (hostSourcesRequest !== request) return;
    const sources = result.targets.filter(source => !asset ||
      (asset.region && source.account === asset.account && source.region === asset.region && source.resource_id === asset.id));
    if (!sources.length) {
      status.textContent = asset ? '此资产没有匹配的只读采集入口。' : '暂无可添加的服务器。请先在服务目录登记只读采集入口。';
      return;
    }
    select.replaceChildren(new Option('请选择采集入口', ''), ...sources.map(source => {
      const option = new Option(source.name + ' · ' + hostProduct(source) + ' · ' + source.service_id, source.id);
      option.dataset.identity = [source.account_label, source.region, source.resource_id].filter(Boolean).join(' · ');
      return option;
    }));
    if (asset && sources.length === 1) select.value = sources[0].id;
    showHostRegistrationIdentity();
    form.hidden = false;
    status.textContent = '';
  } catch {
    if (hostSourcesRequest === request) status.textContent = '无法读取采集入口，请重新打开添加面板。';
  }
}

async function registerHost(event) {
  event.preventDefault();
  if (hostBusy) return;
  const form = event.currentTarget;
  const submit = form.querySelector('button[type=submit]');
  const status = $('host-registration-status');
  submit.disabled = true;
  hostBusy = true;
  try {
    const response = await fetch('/api/host-monitors', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id:$('host-registration-source').value}),
    });
    if (response.status === 404) {
      status.textContent = '采集入口已变更。请重新打开添加面板后选择。';
      return;
    }
    if (!response.ok) throw new Error('Host registration unavailable');
    const result = await response.json();
    if (result.status === 'limit' || result.status === 'ambiguous') {
      status.textContent = result.status === 'limit' ? '已达到 100 个面板登记目标的上限。' : '该资源已有多个监控，请先核对已有登记。';
      return;
    }
    $('host-registration').open = false;
    hostBusy = false;
    await loadHosts();
    if (!hostRows.some(row => row.id === result.id)) await loadHosts();
    if (!hostRows.some(row => row.id === result.id)) {
      status.textContent = '服务器监控已登记，刷新页面后可以查看。';
      return;
    }
    history.replaceState(null, '', '#servers?' + new URLSearchParams({target:result.id}));
    updateNavigation();
    applyResourceRoute();
    status.textContent = result.status === 'existing' ? '已打开原服务器监控。' : '服务器监控已登记，可以开始采集。';
    $('host-run').focus();
    void loadAssets();
  } catch {
    status.textContent = '暂时无法保存服务器监控，请稍后重试。';
  } finally {
    hostBusy = false;
    submit.disabled = false;
  }
}

$('host-registration').addEventListener('toggle', event => {
  if (event.currentTarget.open) loadHostSources();
  else {
    hostSourcesRequest = undefined;
    hostRegistrationAsset = undefined;
  }
});
$('host-registration-source').addEventListener('change', showHostRegistrationIdentity);
$('host-registration-form').addEventListener('submit', registerHost);

$('host-window').addEventListener('change', () => requestHostHistory(currentHost()));
$('host-axis').addEventListener('change', drawHostTrend);
$('host-zoom-reset').addEventListener('click', () => hostPlot?.reset());
$('host-history').addEventListener('toggle', renderHostHistory);
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', drawHostTrend);
$('host-target').addEventListener('change', () => {
  rememberMonitorSelection('servers', $('host-target'));
  $('host-status').textContent = '';
  renderHost(currentHost());
});
$('host-run').addEventListener('click', () => submitHost('run'));
$('host-schedule-form').addEventListener('submit', (event) => {
  event.preventDefault();
  submitHost('schedule', true);
});
$('host-pause').addEventListener('click', () => submitHost('schedule', false));
setInterval(() => loadHosts(true), 5000);
