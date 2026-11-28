const byId = (id) => document.getElementById(id);
const dateTime = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Shanghai',
});
const timeLabel = (stamp) => dateTime.format(new Date(stamp));
const categories = {
  ssh: {order: 0, priority: 'high', title: '公网 SSH 开放'},
  coverage: {order: 1, priority: 'attention', title: '账号漏查'},
  expiry: {order: 2, priority: 'attention', title: '实例到期'},
  balance: {order: 3, priority: 'attention', title: '账号余额不足'},
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

function assetIdentity(asset) {
  return JSON.stringify([asset.kind, asset.account, asset.region, asset.id, asset.scope]);
}

function barFill(count, total, tone) {
  const fill = element('span', 'ops-bar-fill');
  fill.dataset.tone = tone;
  fill.style.inlineSize = (total ? count / total * 100 : 0) + '%';
  return fill;
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

  assets = [...new Map(assets.map((asset) => [assetIdentity(asset), asset])).values()];
  return {item, category, title, target, detail, assets, missing};
}

function queueRow(entry) {
  const {item, category, title, target, detail, assets, missing} = entry;
  const row = element('details', 'ops-incident');
  row.dataset.priority = category.priority;
  const summary = element('summary', 'ops-incident-summary');
  const heading = element('span', 'ops-incident-title', title);
  heading.append(element('span', 'ops-target', target));
  summary.append(heading, badge(priorityLabels[category.priority], category.priority === 'high' ? 'high' : 'neutral'));
  const evidence = element('div', 'ops-evidence');
  evidence.append(element('p', '', detail),
    element('p', '', '最近命中 ' + timeLabel(item.last_seen)));
  if (target !== item.evidence.account_label) evidence.append(element('p', '', item.evidence.account_label));
  if (assets.length) evidence.append(element('p', '', '关联资产 ' + assets.map((asset) => asset.name).join('、')));
  if (missing) evidence.append(element('p', '', assets.length ? '部分资产未匹配' : '未匹配到具体资产'));
  row.append(summary, evidence);
  return row;
}

function monitorCard(name, connected) {
  const card = element('article', 'ops-monitor-card');
  card.dataset.connection = connected ? 'connected' : 'unconnected';
  const header = element('header', 'ops-monitor-head');
  header.append(element('h3', 'ops-signal-name', name), badge(connected ? '已接入' : '未接入', 'neutral'));
  card.append(header);
  return card;
}

function monitorTime(stamp) {
  const time = element('time', 'ops-signal-meta', timeLabel(stamp));
  time.dateTime = stamp;
  time.append(element('span', 'ops-time-zone', ' · 北京时间'));
  return time;
}

function renderLiveHosts(hosts) {
  document.body.classList.add('ops-live-mode');
  document.title = '服务器监控 · StackOrigin';
  byId('ops-mode').textContent = '真实服务器 · 只读';
  byId('ops-title').textContent = '服务器监控';
  byId('heartbeat-title').textContent = '七牛云指标';
  byId('domain-monitoring').hidden = true;
  document.querySelector('.ops-board').hidden = true;
  byId('host-monitor-count').textContent = hosts.length + ' 台已接入';
  const refresh = byId('refresh-monitoring');
  refresh.hidden = false;
  refresh.addEventListener('click', () => location.reload());

  const labels = {
    observed: ['指标已收到', 'success'],
    partial: ['部分指标可用', 'warn'],
    no_data: ['暂无近期指标', 'missing'],
    unavailable: ['查询未成功', 'high'],
  };
  const number = new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 2});
  for (const {name, sample} of hosts) {
    const card = monitorCard(name, true);
    card.append(badge(...labels[sample.status]));
    const values = element('dl', 'ops-live-values');
    for (const [key, label, unit] of [
      ['cpu_percent', 'CPU 使用率', '%'],
      ['network_rx_kbit', '内网接收量', ' kbit'],
      ['network_tx_kbit', '内网发送量', ' kbit'],
    ]) {
      const value = sample.values[key];
      const row = element('div', 'ops-live-metric');
      const amount = element('dd', '', value === undefined ? '未取得' : number.format(value) + unit);
      row.append(element('dt', '', label), amount);
      if (key === 'cpu_percent' && value !== undefined) {
        const gauge = element('meter', 'ops-live-gauge');
        gauge.min = 0;
        gauge.max = 100;
        gauge.value = value;
        gauge.setAttribute('aria-label', label);
        amount.append(gauge);
      }
      values.append(row);
    }
    card.append(values);
    if (sample.observed_at) card.append(monitorTime(sample.observed_at));
    if (sample.reason === 'permission_denied') card.append(element('p', 'ops-signal-meta', '云监控接口未授予读取权限'));
    byId('heartbeats').append(card);
  }
}

function renderWebsite(website) {
  const run = website.observation;
  const dns = {true: ['已解析', 'success'], false: ['解析失败', 'high']};
  const certificates = {
    valid: ['有效', 'success'],
    due: ['即将到期', 'warn'],
    urgent: ['临近到期', 'high'],
    expired: ['已到期', 'high'],
  };
  const unknown = ['未确认', 'missing'];
  const https = run.status === 'pass' ? ['正常', 'success'] :
    run.phases.http?.ok === false ? ['未响应', 'high'] :
    run.status === 'fail' ? ['异常', 'high'] : unknown;
  const card = monitorCard(website.domain, true);
  const stages = element('ol', 'ops-probe-path');
  for (const [name, [status, tone]] of [
    ['DNS', dns[run.phases.dns?.ok] ?? unknown],
    ['证书', certificates[website.certificate.status] ?? unknown],
    ['HTTPS', https],
  ]) {
    const stage = element('li', 'ops-probe-stage');
    stage.dataset.tone = tone;
    if (stages.children.length) stage.append(element('span', 'ops-probe-link'));
    const tile = element('div', 'ops-probe-tile');
    tile.append(element('span', '', name), element('strong', '', status));
    stage.append(tile);
    stages.append(stage);
  }
  card.append(stages, monitorTime(run.observed_at));
  return card;
}

function renderMonitoring({hosts, websites}, assets) {
  const observedAssets = new Set(hosts.filter((host) => host.asset).map((host) => assetIdentity(host.asset)));
  const unconnectedHosts = assets.filter((asset) => asset.kind === 'server' && !observedAssets.has(assetIdentity(asset)));
  const observedDomains = new Set(websites.map((website) => website.domain));
  const unconnectedDomains = assets.filter((asset) => asset.kind === 'domain' && !observedDomains.has(asset.name));
  byId('host-monitor-count').textContent = hosts.length + ' 台已接入 · ' + unconnectedHosts.length + ' 台未接入';
  byId('domain-monitor-count').textContent = websites.length + ' 个已接入 · ' + unconnectedDomains.length + ' 个未接入';

  for (const host of hosts) {
    const card = monitorCard(host.name, true);
    const received = host.observation.status === 'pass';
    const pulse = element('div', 'ops-pulse');
    pulse.dataset.tone = received ? 'success' : 'missing';
    pulse.setAttribute('aria-hidden', 'true');
    pulse.append(element('span', 'ops-wave'), element('span', 'ops-sweep'));
    const status = element('strong', 'ops-heartbeat-status', received ? '心跳已收到' : '采集未确认');
    status.dataset.tone = received ? 'success' : 'missing';
    card.append(status, pulse, monitorTime(host.observation.observed_at));
    byId('heartbeats').append(card);
  }
  byId('domain-signals').append(...websites.map(renderWebsite));

  for (const [target, unconnected, detail] of [
    ['heartbeats', unconnectedHosts, 'account_label'],
    ['domain-signals', unconnectedDomains, 'registrar'],
  ]) {
    for (const asset of unconnected) {
      const card = monitorCard(asset.name, false);
      if (asset[detail]) card.append(element('p', 'ops-signal-meta', asset[detail]));
      byId(target).append(card);
    }
  }
}

function enableMonitoringMotion() {
  const animate = globalThis.Motion?.animate;
  if (!animate) return;
  const settings = matchMedia('(prefers-reduced-motion: reduce)');
  const replay = byId('replay-monitoring');
  const monitor = byId('monitoring');
  const sequence = [];
  for (const sweep of monitor.querySelectorAll('.ops-pulse[data-tone="success"] .ops-sweep')) {
    sequence.push([sweep, {transform: ['translateX(-100%)', 'translateX(100%)'], opacity: [0, 1, 1, 0]},
      {at: 0, duration: 1.55, ease: 'linear'}]);
  }
  for (const path of monitor.querySelectorAll('.ops-probe-path')) {
    [...path.children].forEach((stage, index) => {
      const at = .12 + index * .34;
      const link = stage.querySelector('.ops-probe-link');
      if (link) sequence.push([link, {transform: ['scaleX(0)', 'scaleX(1)']},
        {at: at - .19, duration: .22, ease: 'easeOut'}]);
      sequence.push([stage.querySelector('.ops-probe-tile'),
        {transform: ['translateY(7px)', 'translateY(0)'], opacity: [.55, 1]},
        {at, duration: .38, ease: 'easeOut'}]);
    });
  }
  if (!sequence.length) return;
  let timeline;
  function play() {
    timeline?.cancel();
    if (!settings.matches) timeline = animate(sequence);
  }
  function updatePreference() {
    replay.hidden = settings.matches;
    if (settings.matches) timeline?.cancel();
  }
  settings.addEventListener('change', updatePreference);
  replay.addEventListener('click', play);
  updatePreference();
  play();
}

function render({inspection, items, inventory, monitoring}) {
  renderMonitoring(monitoring, inventory.assets);
  const queue = items.filter((item) => item.status === 'open')
    .map((item) => describeItem(item, inventory.assets))
    .sort((first, second) => first.category.order - second.category.order);
  const high = queue.filter(({category}) => category.priority === 'high').length;
  byId('priority-bar').append(barFill(high, queue.length, 'high'),
    barFill(queue.length - high, queue.length, 'neutral'));

  const rows = queue.map(queueRow);
  byId('queue').replaceChildren(...rows);
  const filters = [...byId('filters').querySelectorAll('button')];
  const totals = {all: queue.length, high, attention: queue.length - high};
  for (const button of filters) button.append(element('span', 'ops-filter-count', ' ' + totals[button.dataset.priority]));
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

  const linked = new Set(queue.flatMap((entry) => entry.assets.map(assetIdentity)));
  byId('asset-count').textContent = linked.size + ' 台';
  const ranked = queue.filter((entry) => entry.assets.length)
    .sort((first, second) => second.assets.length - first.assets.length);
  for (const entry of ranked) {
    const row = element('div', 'ops-impact-row');
    const value = element('dd', 'ops-impact-value');
    const track = element('span', 'ops-impact-track');
    track.setAttribute('aria-hidden', 'true');
    track.append(barFill(entry.assets.length, ranked[0].assets.length, 'reminder'));
    value.append(track, element('span', '', entry.assets.length + ' 项'));
    const label = element('dt', '', entry.title);
    label.append(element('span', 'ops-impact-names', entry.assets.map((asset) => asset.name).join('、')));
    row.append(label, value);
    byId('impact-bars').append(row);
  }
  const withoutAssets = queue.filter((entry) => !entry.assets.length).length;
  byId('asset-note').textContent = withoutAssets
    ? withoutAssets + ' 项异常无具体资产映射'
    : linked.size ? '' : '当前无关联资产';
  byId('asset-note').hidden = !byId('asset-note').textContent;

  const time = byId('inspection-time');
  time.dateTime = inspection.observed_at;
  time.textContent = timeLabel(inspection.observed_at) + ' · 北京时间';
  byId('coverage-label').textContent = inspection.expected_accounts + ' 个应查账号';
  for (const state of ['complete', 'partial', 'unavailable']) {
    const [label, tone] = coverageStates[state];
    const count = inspection.coverage[state];
    byId('coverage-bar').append(barFill(count, inspection.expected_accounts, tone));
    const block = element('div', '');
    block.dataset.tone = tone;
    block.append(element('dt', 'ops-chart-key', label), element('dd', '', count));
    byId('coverage').append(block);
  }
  byId('disabled-accounts').textContent = '另有 ' + inspection.coverage.disabled + ' 个已停用账号';
  for (const account of inspection.accounts) {
    const row = element('li', '', account.label);
    row.append(badge(...coverageStates[account.status]));
    byId('accounts').append(row);
  }
}

try {
  const response = await fetch('/api/ops-preview');
  if (!response.ok) throw new Error('Preview unavailable');
  const snapshot = await response.json();
  if (snapshot.mode === 'live-hosts') {
    renderLiveHosts(snapshot.hosts);
  } else {
    byId('ops-mode').textContent = '模拟数据 · 只读预览';
    render(snapshot);
  }
  byId('preview-state').hidden = true;
  byId('preview').hidden = false;
  if (snapshot.mode !== 'live-hosts') enableMonitoringMotion();
} catch (error) {
  byId('preview-state').textContent = '监控数据读取失败，请刷新页面重试。';
  console.error(error);
}
