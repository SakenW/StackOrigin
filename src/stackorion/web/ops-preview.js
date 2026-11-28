const byId = (id) => document.getElementById(id);
const dateTime = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Shanghai',
});
const timeLabel = (stamp) => dateTime.format(new Date(stamp));
const categories = {
  ssh: {order: 0, priority: 'high', title: '公网 SSH 开放', tone: 'high'},
  coverage: {order: 1, priority: 'attention', title: '账号漏查', tone: 'missing'},
  expiry: {order: 2, priority: 'attention', title: '实例到期', tone: 'reminder'},
  balance: {order: 3, priority: 'attention', title: '账号余额不足', tone: 'warn'},
};
const priorityLabels = {high: '高优先', attention: '需跟进'};
const coverageStates = {
  complete: ['查完', 'success'],
  partial: ['部分完成', 'warn'],
  unavailable: ['未查到', 'missing'],
  disabled: ['已停用', 'neutral'],
};

function element(tag, className, content) {
  const node = document.createElement(tag);
  node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}

function badge(text, tone) {
  const node = element('span', 'ops-badge', text);
  node.dataset.tone = tone;
  return node;
}

function uniqueAsset(assets, matches) {
  const candidates = assets.filter(matches);
  return candidates.length === 1 ? candidates[0] : null;
}

function describeItem(item, inventory) {
  const evidence = item.evidence;
  const category = categories[evidence.kind];
  const scopedServers = inventory.filter((asset) => asset.kind === 'server' &&
    asset.account === evidence.account && asset.region === evidence.region);
  let assets = [];
  let missing = false;
  let title = category.title;
  let target = evidence.account_label;
  let detail;

  switch (evidence.kind) {
    case 'ssh': {
      const names = [...new Set(evidence.instances)];
      assets = names.map((name) => uniqueAsset(scopedServers, (asset) => asset.name === name)).filter(Boolean);
      missing = !names.length || assets.length !== names.length;
      target = evidence.name;
      const ports = evidence.first_port === evidence.last_port
        ? evidence.first_port : evidence.first_port + '–' + evidence.last_port;
      detail = '入站允许 ' + evidence.protocol.toUpperCase() + ' ' + ports + ' · 来源 ' + evidence.source;
      break;
    }
    case 'expiry': {
      const asset = uniqueAsset(scopedServers, (row) => row.id === evidence.id);
      if (asset) assets = [asset];
      missing = !asset;
      title = {due: '实例即将到期', expired: '实例已到期', unknown: '实例到期日未知'}[evidence.status];
      target = evidence.name;
      detail = evidence.expires_at ? '到期时间 ' + timeLabel(evidence.expires_at) : '未取得到期时间';
      break;
    }
    case 'coverage':
      title = evidence.status === 'partial' ? '账号巡检未查全' : '账号巡检未查到';
      detail = evidence.reason;
      break;
    case 'balance':
      detail = '余额 ' + evidence.amount + ' ' + evidence.currency +
        ' · 提醒线 ' + evidence.threshold + ' ' + evidence.currency;
      break;
  }

  return {item, category, title, target, detail, assets, missing};
}

function queueRow(entry) {
  const {item, category, title, target, detail, assets, missing} = entry;
  const row = element('details', 'ops-incident');
  row.dataset.priority = category.priority;
  const summary = element('summary', 'ops-incident-summary');
  const heading = element('span', 'ops-incident-title', title);
  heading.append(element('span', 'ops-target', target));
  const association = assets.length ? assets.length + ' 项关联' + (missing ? ' · 映射不全' : '') :
    missing ? '未匹配资产' : '账号级 · 无资产映射';
  summary.append(heading, badge(priorityLabels[category.priority], category.tone),
    element('span', 'ops-association', association));
  const evidence = element('div', 'ops-evidence');
  evidence.append(element('p', '', detail),
    element('p', '', '最近命中 ' + timeLabel(item.last_seen)));
  if (target !== item.evidence.account_label) evidence.append(element('p', '', item.evidence.account_label));
  if (assets.length) evidence.append(element('p', '', '关联资产 ' + assets.map((asset) => asset.name).join('、')));
  row.append(summary, evidence);
  return row;
}

function render({inspection, items, inventory}) {
  const queue = items.filter((item) => item.status === 'open')
    .map((item) => describeItem(item, inventory.assets))
    .sort((first, second) => first.category.order - second.category.order);
  const high = queue.filter(({category}) => category.priority === 'high').length;
  const linked = new Map();
  for (const entry of queue) {
    for (const asset of entry.assets) {
      const titles = linked.get(asset) || [];
      if (!titles.includes(entry.title)) titles.push(entry.title);
      linked.set(asset, titles);
    }
  }

  byId('pending').textContent = queue.length;
  byId('priority').textContent = high + ' 项高优先 · ' + (queue.length - high) + ' 项需跟进';
  byId('impact').textContent = linked.size;
  byId('inventory-note').textContent = '固定盘点共 ' + inventory.assets.length + ' 项';
  byId('complete').append(String(inspection.coverage.complete),
    element('span', 'ops-denominator', ' / ' + inspection.expected_accounts));
  byId('snapshot-note').textContent = timeLabel(inspection.observed_at) + ' · 北京时间';

  const rows = queue.map(queueRow);
  byId('queue').replaceChildren(...rows);
  const filters = [...byId('filters').querySelectorAll('button')];
  function filter(selected) {
    let visible = 0;
    for (const row of rows) {
      row.hidden = selected.dataset.priority !== 'all' && row.dataset.priority !== selected.dataset.priority;
      if (!row.hidden) visible += 1;
    }
    filters.forEach((button) => button.setAttribute('aria-pressed', button === selected));
    byId('queue-count').textContent = '显示 ' + visible + ' 项待处理异常';
    byId('queue-empty').hidden = visible > 0;
  }
  for (const button of filters) button.addEventListener('click', () => filter(button));
  filter(filters[0]);

  byId('asset-count').textContent = linked.size + ' / ' + inventory.assets.length + ' 项';
  for (const [asset, titles] of linked) {
    const row = element('li', '');
    const identity = element('div', 'ops-asset-name', asset.name);
    identity.append(element('small', '', asset.account_label));
    row.append(identity, element('span', 'ops-asset-signal', titles.join('、')));
    byId('asset-list').append(row);
  }
  const withoutAssets = queue.filter((entry) => !entry.assets.length).length;
  byId('asset-note').textContent = withoutAssets
    ? withoutAssets + ' 项异常未关联具体资产'
    : linked.size ? '仅统计待处理异常中已确认的关联' : '当前无关联资产';

  const time = byId('inspection-time');
  time.dateTime = inspection.observed_at;
  time.textContent = timeLabel(inspection.observed_at) + ' · 北京时间';
  byId('coverage-label').textContent = inspection.expected_accounts + ' 个应查账号';
  for (const [state, [label, tone]] of Object.entries(coverageStates)) {
    const block = element('div', '');
    block.dataset.tone = tone;
    block.append(element('dt', '', label), element('dd', '', inspection.coverage[state]));
    byId('coverage').append(block);
  }
  for (const account of inspection.accounts) {
    const row = element('li', '', account.label);
    row.append(badge(...coverageStates[account.status]));
    byId('accounts').append(row);
  }
}

try {
  const response = await fetch('/api/ops-preview');
  if (!response.ok) throw new Error('Preview unavailable');
  render(await response.json());
  byId('preview-state').hidden = true;
  byId('preview').hidden = false;
} catch (error) {
  byId('preview-state').textContent = '模拟快照读取失败，请刷新页面重试。';
  console.error(error);
}
