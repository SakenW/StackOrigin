const get = id => document.getElementById(id);
const form = get('resource-preview-form');
const dialog = get('operation-confirm');
const execute = get('operation-execute');
const verify = get('operation-verify');
const summary = get('operation-summary');
const history = get('resource-operation-history');
const historyRows = get('operation-history-rows');
const historyStatus = get('operation-history-status');
const clock = new Intl.DateTimeFormat('zh-CN', {dateStyle:'medium', timeStyle:'short', timeZone:'Asia/Shanghai'});
let active;
let current;

export function showResourceOperation(binding, workItemId = null) {
  active = binding && {...binding, workItemId};
  get('resource-operation').hidden = !binding;
  if (!binding) return;
  form.reset();
  form.hidden = binding.preview_available === false;
  history.open = false;
  historyRows.replaceChildren();
  historyStatus.textContent = '';
  const dns = binding.action === 'dns';
  get('resource-operation-heading').textContent = dns ? '新增一条 A 记录' : 'QVM 重启';
  get('resource-operation-mode').textContent = binding.preview_available === false ? '连接已停用 · 可查历史' : binding.simulation ? '合成演练' : '只读预览';
  get('resource-operation-binding').textContent = '资源操作编号 ' + binding.id;
  const button = get('resource-preview-button');
  button.textContent = workItemId ? '生成关联预览' : '生成预览';
  button.disabled = false;
  get('resource-dns-fields').hidden = !dns;
  for (const field of get('resource-dns-fields').querySelectorAll('input')) {
    field.disabled = !dns;
    field.required = dns;
  }
  get('resource-operation-status').textContent = '';
}

async function request(action, data) {
  const response = await fetch('/api/resource-operations/' + action, {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data),
  });
  if (!response.ok) throw new Error('Operation unavailable');
  return await response.json();
}

const labels = {instance:'实例 ID', region:'地域', current:'当前状态', zone:'Zone ID',
  domain:'域名', zone_status:'Zone 当前状态', existing_a:'同名已存在的 A 记录',
  name:'记录名', content:'IPv4 地址', ttl:'TTL（秒）', force_stop:'强制停止'};
const statuses = {preview:'仅预览', accepted:'已受理', success:'已验证', 'needs-reconciliation':'待核对'};

function summarize(record) {
  const {status, result} = record;
  get('operation-confirm-title').textContent = status === 'preview' ? '操作预览' : '操作回执';
  const parts = [statuses[status] || statuses['needs-reconciliation']];
  if (record.preview?.simulation) parts.push('模拟');
  if (result?.record_verified !== undefined) {
    parts.push(result.record_verified ? '记录已读回' : '记录未验证');
    parts.push('公网传播未验证');
  } else if (result?.current) {
    parts.push('主机状态 ' + result.current, '重启完成未验证');
  } else if (record.preview?.action === 'reboot' && status !== 'preview') {
    parts.push('重启完成未验证');
  }
  if (result?.operation_id) parts.push('回执 ' + result.operation_id);
  summary.textContent = parts.join(' · ');
}

function openPlan(plan, record = null, fromLedger = false, fromLink = false) {
  const fresh = !record;
  get('operation-confirm-title').textContent = '操作预览';
  if (record?.confirmable) plan = {...plan, plan_id:plan.record_id};
  current = {plan, fromLedger, fromLink, operation: record && record.status !== 'preview' ? plan.record_id : null};
  const fields = {'操作':plan.action === 'dns' ? '新增 · A · 仅 DNS' : '普通重启',
    '连接':plan.service, '账号':plan.account, '操作编号':plan.record_id};
  if (plan.work_item_id) fields['关联待办'] = plan.work_item_id;
  for (const [key, value] of Object.entries(plan.display)) {
    fields[labels[key]] = typeof value === 'boolean' ? (value ? '是' : '否') : value;
  }
  get('operation-plan').replaceChildren(...Object.entries(fields).map(([name, value]) => {
    const line = document.createElement('div');
    for (const [tag, text] of [['dt', name], ['dd', value]]) {
      const node = document.createElement(tag);
      node.textContent = text;
      line.append(node);
    }
    return line;
  }));
  summary.textContent = (plan.simulation ? '仅预览 · 合成演练。' : '仅预览。') +
    (plan.action === 'dns' ? '控制面核对与公网 DNS 传播分别记录。' : '重启期间服务可能短暂中断。');
  execute.hidden = !(fresh ? plan.simulation : record.confirmable);
  execute.disabled = false;
  verify.hidden = !plan.simulation || !current.operation;
  const resourceLink = get('operation-resource-link');
  resourceLink.hidden = !fromLedger;
  if (fromLedger) resourceLink.href = '#assets?' + new URLSearchParams({operation:plan.binding});
  const workLink = get('operation-work-link');
  workLink.hidden = !fromLedger || !plan.work_item_id;
  if (!workLink.hidden) workLink.href = '#work-items?' + new URLSearchParams({item:plan.work_item_id});
  if (record) summarize(record);
  dialog.showModal();
}

async function loadRecords(binding) {
  const requested = binding;
  historyStatus.textContent = '正在读取记录…';
  try {
    const response = await fetch('/api/resource-operations/' + encodeURIComponent(binding.id) + '/history');
    if (!response.ok) throw new Error('History unavailable');
    const {records} = await response.json();
    if (active !== requested) return;
    const focused = document.activeElement?.dataset.record;
    historyRows.replaceChildren(...records.map(record => {
      const row = document.createElement('li');
      const control = document.createElement('button');
      control.type = 'button';
      control.className = 'monitor-secondary';
      control.dataset.record = record.preview.record_id;
      const state = document.createElement('strong');
      state.textContent = statuses[record.status] || statuses['needs-reconciliation'];
      const time = document.createElement('time');
      time.dateTime = record.created_at;
      time.textContent = clock.format(new Date(record.created_at)) + ' UTC+8';
      control.append(state, time);
      control.addEventListener('click', () => openPlan(record.preview, record));
      row.append(control);
      return row;
    }));
    if (focused && history.open) [...historyRows.querySelectorAll('button')].find(node => node.dataset.record === focused)?.focus();
    historyStatus.textContent = records.length ? '最近 ' + records.length + ' 条' : '暂无操作记录';
  } catch {
    if (active === requested) historyStatus.textContent = '无法读取操作记录，请重新展开重试。';
  }
}

history.addEventListener('toggle', () => {
  if (history.open && active) loadRecords(active);
});
dialog.addEventListener('close', () => {
  if (get('details').open && history.open && active) loadRecords(active);
  if (current?.fromLedger && location.hash.split('?')[0] === '#operation-records') {
    if (current.fromLink) window.history.replaceState(null, '', '#operation-records');
    loadLedger();
  }
});
for (const id of ['operation-resource-link', 'operation-work-link']) {
  get(id).addEventListener('click', event => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (current) current.fromLedger = false;
    dialog.close();
  });
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  const binding = active;
  if (!binding || form.hidden) return;
  const status = get('resource-operation-status');
  const button = get('resource-preview-button');
  if (button.disabled) return;
  button.disabled = true;
  status.textContent = '正在读取当前状态…';
  try {
    const data = {binding:binding.id};
    if (binding.action === 'dns') Object.assign(data, {
      name:form.elements.name.value.trim(), content:form.elements.content.value.trim(), ttl:form.elements.ttl.valueAsNumber,
    });
    if (binding.workItemId) data.work_item_id = binding.workItemId;
    const plan = await request(binding.workItemId ? 'handoff' : 'preview', data);
    if (active !== binding || !get('details').open) return;
    button.disabled = false;
    button.focus();
    openPlan(plan);
    status.textContent = '';
  } catch {
    if (active === binding) status.textContent = binding.workItemId ?
      '无法生成关联预览。请重新打开待办并核对资源绑定和权限。' :
      '无法生成预览。请核对目标配置、权限和填写内容。';
  } finally {
    if (active === binding) button.disabled = false;
  }
});

execute.addEventListener('click', async () => {
  if (!current?.plan.plan_id || execute.disabled) return;
  const session = current;
  execute.disabled = true;
  summary.textContent = '正在提交模拟操作…';
  session.operation = session.plan.plan_id;
  try {
    const result = await request('execute', {plan_id:session.operation, confirmed:true});
    if (session === current) summarize({status:result.status, result, preview:session.plan});
  } catch {
    if (session === current) summarize({status:'needs-reconciliation',
      result:{operation_id:session.operation}, preview:session.plan});
  } finally {
    if (session === current) {
      execute.hidden = true;
      verify.hidden = false;
      if (dialog.open) verify.focus();
    }
  }
});

verify.addEventListener('click', async () => {
  const session = current;
  verify.disabled = true;
  try {
    const result = await request('verify', {operation_id:session.operation});
    if (session !== current) return;
    summarize({status:result.status, result, preview:session.plan});
  } catch {
    if (session === current) summary.textContent = '暂时读不到回执，仍待核对 · ' + session.operation;
  } finally {
    verify.disabled = false;
  }
});

const ledgerFilters = get('operation-record-filters');
const ledgerRows = get('operation-ledger-rows');
const ledgerStatus = get('operation-ledger-status');
let ledgerRequest;
const actionLabels = {reboot:'QVM 重启', dns:'新增 A 记录'};

async function loadLinkedRecord(id, fromLink = false) {
  const hash = location.hash;
  if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(id)) {
    ledgerStatus.textContent = '操作编号无效，请从操作记录列表查看。';
    return;
  }
  ledgerStatus.textContent = '正在读取指定操作记录…';
  try {
    const response = await fetch('/api/resource-operations/record/' + id);
    if (!response.ok) throw new Error('Record unavailable');
    const record = await response.json();
    if (location.hash !== hash || dialog.open) return;
    if (fromLink) get('operation-records-title').focus();
    openPlan(record.preview, record, true, fromLink);
    ledgerStatus.textContent = '';
  } catch {
    if (location.hash === hash) ledgerStatus.textContent = '无法打开这条操作记录，请核对编号或从列表查看。';
  }
}

function showLedgerRow(record) {
  const li = document.createElement('li');
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'row operation-ledger-row';
  const resource = document.createElement('span');
  const name = document.createElement('strong');
  name.textContent = record.resource.name;
  const context = document.createElement('small');
  context.textContent = [actionLabels[record.preview.action], record.resource.account,
    ...(record.preview.simulation ? ['模拟'] : [])].join(' · ');
  resource.append(name, context);
  const status = document.createElement('span');
  status.className = 'result';
  status.dataset.tone = ({preview:'neutral', accepted:'reminder', success:'success',
    'needs-reconciliation':'warn'})[record.status];
  status.textContent = statuses[record.status] || statuses['needs-reconciliation'];
  const time = document.createElement('time');
  time.dateTime = record.created_at;
  time.textContent = clock.format(new Date(record.created_at)) + ' UTC+8';
  button.append(resource, status, time);
  button.dataset.record = record.preview.record_id;
  button.addEventListener('click', () => {
    if (record.preview.handoff) loadLinkedRecord(record.preview.record_id);
    else openPlan(record.preview, record, true);
  });
  li.append(button);
  return li;
}

async function loadLedger() {
  ledgerRequest?.abort();
  const controller = new AbortController();
  ledgerRequest = controller;
  const query = new URLSearchParams(new FormData(ledgerFilters));
  ledgerStatus.textContent = '正在读取操作记录…';
  try {
    const response = await fetch('/api/resource-operations/history?' + query, {signal:controller.signal});
    if (!response.ok) throw new Error('Operation history unavailable');
    const data = await response.json();
    if (controller.signal.aborted) return;
    const select = get('operation-filter-resource');
    const selected = select.value;
    select.replaceChildren(new Option('全部资源', ''), ...data.resources.map(resource =>
      new Option(resource.name + ' · ' + resource.account, resource.id)));
    if (data.resources.some(resource => resource.id === selected)) select.value = selected;
    else if (selected) return loadLedger();
    const focused = document.activeElement?.dataset.record;
    ledgerRows.replaceChildren(...data.records.map(showLedgerRow));
    if (focused) [...ledgerRows.querySelectorAll('button')].find(node => node.dataset.record === focused)?.focus();
    ledgerStatus.textContent = !data.available ? '当前面板未配置操作预览。' :
      data.records.length ? data.records.length + ' 条记录' + (data.truncated ? ' · 仅显示最新 100 条' : '') :
        '没有符合筛选条件的操作记录。';
  } catch {
    if (controller.signal.aborted) return;
    ledgerRows.replaceChildren();
    ledgerStatus.textContent = '无法读取操作记录，请返回该页面或调整筛选重试。';
  }
}

ledgerFilters.addEventListener('change', loadLedger);
ledgerFilters.addEventListener('submit', event => { event.preventDefault(); loadLedger(); });
function openLedgerRoute() {
  const [section, query] = location.hash.split('?');
  if (section !== '#operation-records') return;
  const id = new URLSearchParams(query).get('record');
  if (id) loadLinkedRecord(id, true);
  else loadLedger();
}
window.addEventListener('hashchange', openLedgerRoute);
openLedgerRoute();
