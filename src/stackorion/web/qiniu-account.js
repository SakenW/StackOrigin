const panel = document.getElementById('qiniu-account');
const status = document.getElementById('qiniu-account-status');
const content = document.getElementById('qiniu-account-rows');
const lasStates = {running:'运行中', stopped:'已停止', starting:'启动中', stopping:'停止中'};
const cdnStates = {success:'已上线', online:'已上线', offline:'已下线'};
const payments = {paid:'已支付', unpaid:'未支付', refunded:'已退款', postpaid:'已转后付费'};

function node(tag, text) {
  const item = document.createElement(tag);
  item.textContent = text;
  return item;
}

function group(title, lines, note = '') {
  const details = document.createElement('details');
  details.className = 'host-history';
  details.append(node('summary', title));
  if (note) details.append(node('p', note));
  if (lines.length) {
    const list = document.createElement('ul');
    list.append(...lines.map((line) => node('li', line)));
    details.append(list);
  }
  return details;
}

function failed(label, result) {
  return node('p', label + (result.status === 'permission_denied' ? ' · 未授权读取' : ' · 暂时读不到'));
}

function render(data) {
  const regions = Object.entries(data.las);
  const instances = regions.flatMap(([, result]) => result.data ?? []);
  const missing = regions.filter(([, result]) => result.status !== 'observed').map(([name]) => name);
  const coverage = '成功读取 ' + (regions.length - missing.length) + '/' + regions.length + ' 个配置地域'
    + (missing.length ? ' · 未取得 ' + missing.join('、') : '');
  const sections = [group('轻量服务器 · 已读到 ' + instances.length + ' 台', instances.map((row) =>
    [row.name, row.region, lasStates[row.state] ?? row.state, row.expires_at ? '到期 ' + row.expires_at.slice(0, 10) : ''].filter(Boolean).join(' · ')), coverage)];
  sections.push(data.kodo.data
    ? group('对象存储 · ' + data.kodo.data.length + ' 个存储桶', data.kodo.data)
    : failed('对象存储', data.kodo));
  sections.push(data.cdn.data
    ? group('CDN · ' + data.cdn.data.length + ' 个加速域名', data.cdn.data.map((row) =>
      [row.name, cdnStates[row.state] ?? row.state, row.protocol?.toUpperCase()].filter(Boolean).join(' · ')))
    : failed('CDN', data.cdn));
  const balance = data.balance.data;
  const bill = data.bill.data;
  sections.push(balance
    ? node('p', '账户可用余额 · ' + balance.available + ' ' + balance.currency)
    : failed('账户余额', data.balance));
  sections.push(bill
    ? group(bill.period + ' 月结算单 · ' + bill.rows.length + ' 条', bill.rows.map((row) =>
      [row.product, row.item, {order:'订单', bill:'账单'}[row.type] ?? row.type, payments[row.payment] ?? row.payment,
        row.fee + ' ' + row.currency].filter(Boolean).join(' · ')))
    : failed('月结算单', data.bill));
  content.replaceChildren(...sections);
  status.textContent = '更新于 ' + new Intl.DateTimeFormat('zh-CN', {
    timeZone:'Asia/Shanghai', dateStyle:'short', timeStyle:'medium',
  }).format(new Date(data.observed_at)) + '（北京时间）';
}

let loading = false;
panel.addEventListener('toggle', async () => {
  if (!panel.open || loading) return;
  loading = true;
  try {
    const response = await fetch('/api/qiniu-account');
    if (response.status === 404) {
      content.replaceChildren();
      status.textContent = '当前工作台未配置七牛云账号读取';
      return;
    }
    if (!response.ok) throw new Error('Account unavailable');
    render(await response.json());
  } catch {
    content.replaceChildren();
    status.textContent = '读取失败，重新展开可重试';
  } finally {
    loading = false;
  }
});
