const get = id => document.getElementById(id);
const form = get('onboarding-form');
const selector = get('onboarding-provider');
const state = get('onboarding-state');
const details = get('onboarding-results');
const save = get('onboarding-save');
const test = get('onboarding-test');
let settings;
let busy = false;

const labels = {
  observed:'只读连接已验证', partial:'只读连接可用，读取范围不完整',
  unauthorized:'鉴权失败，请核对凭据是否有效',
  denied:'所需权限或资源授权范围不足',
  mismatch:'域名、账号或 Zone 与返回数据不一致，请核对配置',
  unavailable:'暂时无法验证，请检查网络或服务状态',
  already_configured:'这个 Zone 或 API Key 已接入，请在下方查看已有连接。',
  connection_name_used:'此服务商已使用该连接名称，请换个名称。',
};

function clearSecrets() {
  for (const input of form.querySelectorAll('input[type="password"]')) input.value = '';
}

function renderProvider() {
  const provider = settings?.providers.find(row => row.id === selector.value);
  if (!provider) return;
  for (const id of ['cloudflare','spaceship']) {
    const fields = get('onboarding-' + id);
    fields.hidden = fields.disabled = id !== provider.id;
  }
  get('onboarding-scope').textContent = '需要 ' + provider.required_scope + '。' +
    (provider.id === 'cloudflare' ? '在域名概览查找 Account ID 和 Zone ID，并把 Token 授权给该 Zone。' :
      '使用为域名读取创建的 API Key 和 Secret。');
  save.disabled = true;
  details.hidden = true;
}

const checkedClock = new Intl.DateTimeFormat('zh-CN', {
  dateStyle:'medium', timeStyle:'short', timeZone:'Asia/Shanghai',
});

function showCheck(result, detail, status) {
  renderResult(result, detail);
  const dns = result.dns?.scope && result.dns;
  const summary = dns ? dns.target + ' · ' + dnsVerification[dns.status] +
    (dns.count === undefined ? '' : ' · ' + dns.count + ' 条记录') : labels[result.status];
  status.textContent = summary + ' · ' + checkedClock.format(new Date(result.checked_at)) + ' UTC+8';
}

const dnsVerification = {observed:'已验证', partial:'部分读取', denied:'权限不足',
  unauthorized:'鉴权失败', unavailable:'未能验证'};

function renderDnsRecords(dns, container) {
  container.replaceChildren();
  container.hidden = !dns?.records?.length;
  if (container.hidden) return;
  const table = document.createElement('table');
  table.createCaption().textContent = 'DNS 记录明细' +
    (dns.status === 'partial' ? '（已读取 ' + dns.records.length + ' / ' + dns.count + ' 条）' : '');
  const header = table.createTHead().insertRow();
  for (const label of ['记录名', '类型', 'TTL（秒）']) {
    const cell = document.createElement('th');
    cell.scope = 'col';
    cell.textContent = label;
    header.append(cell);
  }
  const body = table.createTBody();
  for (const record of dns.records) {
    const row = body.insertRow();
    for (const value of [record.name || '@', record.type, record.ttl ?? '未提供']) row.insertCell().textContent = value;
  }
  container.append(table);
}

function addConnectionCheck(row, actions, connection, outcome) {
  const isSpaceship = connection.adapter === 'spaceship-domains';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'monitor-secondary';
  button.textContent = isSpaceship ? '核对权限并加载域名' : '核对只读权限';
  button.setAttribute('aria-label', button.textContent + ' ' + (connection.label || connection.id));
  const detail = document.createElement('dl');
  detail.className = 'onboarding-results';
  detail.hidden = true;
  actions.append(button);
  row.append(detail);

  const controls = document.createElement('div');
  controls.className = 'onboarding-dns-controls';
  controls.hidden = true;
  const label = document.createElement('label');
  label.textContent = '选择域名';
  const select = document.createElement('select');
  label.append(select);
  const read = document.createElement('button');
  read.type = 'button';
  read.className = 'monitor-secondary';
  read.textContent = '核对 DNS 记录';
  controls.append(label, read);
  const records = document.createElement('div');
  records.className = 'onboarding-dns-records';
  records.hidden = true;
  if (isSpaceship) row.append(controls, records);
  let lastResult;

  function present(result) {
    lastResult = result;
    showCheck(result, detail, outcome);
    if (!isSpaceship) return;
    const domains = result.domains || [];
    const placeholder = new Option('请选择域名', '');
    placeholder.disabled = true;
    select.replaceChildren(placeholder, ...domains.map(domain => new Option(domain, domain)));
    select.value = result.dns?.target || '';
    controls.hidden = !domains.length;
    read.disabled = !select.value;
    if (domains.length && !result.dns) outcome.textContent += '。请选择要核对的域名。';
    renderDnsRecords(result.dns, records);
  }

  async function check(domain) {
    const payload = {service_id:connection.id};
    if (domain) payload.domain = domain;
    button.disabled = select.disabled = read.disabled = true;
    detail.hidden = records.hidden = true;
    outcome.textContent = domain ? '正在核对 ' + domain + ' 的 DNS 记录…' : '正在核对已登记的连接…';
    try {
      const response = await fetch('/api/onboarding/check', {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload),
      });
      if (!response.ok) throw new Error('Connection check unavailable');
      present(await response.json());
    } catch {
      detail.hidden = records.hidden = true;
      outcome.textContent = '无法读取连接状态，请重新核对只读权限或稍后重试。';
    } finally {
      button.disabled = select.disabled = false;
      read.disabled = !select.value;
    }
  }

  button.addEventListener('click', () => check());
  select.addEventListener('change', () => {
    read.disabled = !select.value;
    if (select.value === lastResult.dns?.target) {
      showCheck(lastResult, detail, outcome);
      renderDnsRecords(lastResult.dns, records);
      return;
    }
    const {dns, ...connectionResult} = lastResult;
    renderResult({...connectionResult, unverified_scope: dns?.scope ?? lastResult.unverified_scope}, detail);
    renderDnsRecords(null, records);
    outcome.textContent = select.value + ' · 尚未核对 DNS 记录';
  });
  read.addEventListener('click', () => check(select.value));
  if (connection.last_check) present(connection.last_check);
}

function renderConnection(connection) {
  const row = document.createElement('li');
  const title = document.createElement('strong');
  row.dataset.inspectionAccount = 'live:' + connection.provider + ':' + (connection.account_id || connection.id);
  title.tabIndex = -1;
  const name = connection.label || connection.id;
  title.textContent = settings.providers.find(provider => provider.id === connection.provider)?.label
    + ' · ' + name + (settings.editable ? ' · 只读' : '');
  const outcome = document.createElement('p');
  outcome.setAttribute('role', 'status');
  outcome.textContent = !connection.enabled ? '连接已停用' :
    connection.can_check ? '权限和资源尚未核对' : '已启用 · 此连接未执行权限检查';
  row.append(title, outcome);
  const actions = document.createElement('div');
  actions.className = 'operation-buttons';
  if (connection.can_check || connection.can_manage) row.append(actions);
  if (connection.can_check) addConnectionCheck(row, actions, connection, outcome);
  if (connection.can_manage) {
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'monitor-secondary';
    toggle.dataset.connectionState = '';
    toggle.textContent = connection.enabled ? '停用连接' : '恢复连接';
    toggle.setAttribute('aria-label', toggle.textContent + ' ' + name);
    toggle.addEventListener('click', async () => {
      toggle.disabled = true;
      outcome.textContent = connection.enabled ? '正在停用…' : '正在恢复…';
      try {
        const response = await fetch('/api/onboarding/state', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({service_id:connection.id, enabled:!connection.enabled}),
        });
        if (!response.ok) throw new Error('Connection state unavailable');
        const updated = await response.json();
        settings.connections = settings.connections.map(entry => entry.id === updated.id ? updated : entry);
        const replacement = renderConnection(updated);
        const restoreFocus = document.activeElement === toggle || document.activeElement === document.body;
        if (row.isConnected) {
          row.replaceWith(replacement);
          if (restoreFocus) replacement.querySelector('[data-connection-state]').focus();
        } else {
          renderConnections();
        }
        state.textContent = updated.enabled ? name + ' 已恢复，可参加下次巡检。' : name + ' 已停用，凭据仍保存在本机。';
        window.dispatchEvent(new Event('stackorion:connection-state-changed'));
      } catch {
        outcome.textContent = '未能确认连接状态，请刷新核对。';
      } finally {
        toggle.disabled = false;
      }
    });
    actions.append(toggle);
    if (connection.enabled) addCredentialUpdate(row, connection);
  }
  return row;
}

function addCredentialUpdate(row, connection) {
  const isSpaceship = connection.provider === 'spaceship';
  const credential = isSpaceship ? 'API Secret' : 'API Token';
  const panel = document.createElement('details');
  panel.className = 'onboarding-credential';
  const summary = document.createElement('summary');
  summary.textContent = '更换 ' + credential;
  const fields = document.createElement('form');
  const hint = document.createElement('p');
  hint.id = 'credential-help-' + connection.id;
  hint.textContent = isSpaceship ? '使用此连接原来的 API Key 测试新 Secret。' : '新 Token 需要读取当前连接的账号和 Zone。';
  const input = document.createElement('input');
  input.id = 'credential-value-' + connection.id;
  input.type = 'password';
  input.autocomplete = 'off';
  input.required = true;
  input.maxLength = 512;
  input.setAttribute('aria-describedby', hint.id);
  const label = document.createElement('label');
  label.htmlFor = input.id;
  label.textContent = '新 ' + credential;
  const submit = document.createElement('button');
  submit.type = 'submit';
  submit.className = 'monitor-secondary';
  submit.textContent = '验证并保存';
  const outcome = document.createElement('p');
  outcome.setAttribute('role', 'status');
  fields.append(hint, label, input, submit, outcome);
  panel.append(summary, fields);
  row.append(panel);

  fields.addEventListener('submit', async event => {
    event.preventDefault();
    if (submit.disabled) return;
    input.disabled = submit.disabled = true;
    outcome.textContent = '正在验证新凭据…';
    try {
      const response = await fetch('/api/onboarding/credential', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({service_id:connection.id, credential:input.value}),
      });
      if (!response.ok) throw new Error('Credential update unavailable');
      const result = await response.json();
      if (!result.saved) {
        const failures = {
          key_unavailable:'本机无法读取原 API Key。',
          connection_changed:'连接已变化，请刷新后重新核对。',
        };
        outcome.textContent = (failures[result.status] || labels[result.status] || '无法验证新凭据。') + ' 本次未保存。';
        return;
      }
      fields.reset();
      const current = settings.connections.find(entry => entry.id === connection.id);
      delete current.last_check;
      const replacement = renderConnection(current);
      if (row.isConnected) {
        row.replaceWith(replacement);
        replacement.querySelector('.onboarding-credential summary')?.focus();
      } else {
        renderConnections();
      }
      state.textContent = (connection.label || connection.id) + ' 的新凭据已验证并保存。';
    } catch {
      outcome.textContent = '未能确认更新结果，请刷新后核对只读权限。';
    } finally {
      input.disabled = submit.disabled = false;
    }
  });
}

function renderConnections() {
  get('onboarding-connection-list').replaceChildren(...settings.connections.map(renderConnection));
  get('onboarding-state-help').hidden = !settings.connections.some(connection => connection.can_manage);
  get('onboarding-connections').hidden = !settings.connections.length;
}

async function loadOnboarding() {
  try {
    const response = await fetch('/api/onboarding');
    if (!response.ok) throw new Error('Onboarding unavailable');
    settings = await response.json();
    renderConnections();
    if (!settings.editable) {
      state.textContent = '正在使用已登记的服务目录，可对下列连接发起只读核对。';
      return;
    }
    const selected = selector.value;
    selector.replaceChildren(...settings.providers.map(row => new Option(row.label, row.id)));
    if (settings.providers.some(row => row.id === selected)) selector.value = selected;
    form.hidden = false;
    state.textContent = '凭据保存在本机私有目录。';
    renderProvider();
  } catch {
    state.textContent = '无法读取接入设置，请刷新后重试。';
  }
}

function renderResult(result, target = details) {
  const usable = ['observed','partial'].includes(result.status);
  const fields = [];
  if (result.required_scope) fields.push(['只读权限', result.required_scope + ' · ' +
    (usable ? '已验证' : result.status === 'denied' ? '授权不足或不在授权范围' : '未验证')]);
  if (result.count !== null && result.count !== undefined) {
    fields.push(['可查资源', result.count + (result.resource_unit || (result.provider === 'cloudflare' ? ' 个已登记 Zone' : ' 个域名'))]);
    if (result.names.length) fields.push(['资源样例', result.names.join('、')]);
  }
  if (result.unverified_scope) fields.push(['未验证权限', result.unverified_scope + '（DNS 记录读取）']);
  if (result.dns) {
    const dns = result.dns;
    fields.push(['DNS 读取权限', [dns.scope, dnsVerification[dns.status], dns.target,
      dns.count === undefined ? null : dns.count + ' 条记录'].filter(Boolean).join(' · ')]);
  }
  fields.push(['云端变更权限', '未测试']);
  target.replaceChildren(...fields.map(([label, value]) => {
    const row = document.createElement('div');
    for (const [tag, text] of [['dt', label], ['dd', value]]) {
      const node = document.createElement(tag);
      node.textContent = text;
      row.append(node);
    }
    return row;
  }));
  target.hidden = false;
  return usable;
}

async function submit(action) {
  if (busy || !form.reportValidity()) return;
  const payload = Object.fromEntries(new FormData(form));
  const fields = [...form.querySelectorAll('input')].filter(input => !input.matches(':disabled'));
  busy = test.disabled = save.disabled = selector.disabled = true;
  for (const input of fields) input.disabled = true;
  state.textContent = action === 'save' ? '正在复查并保存…' : '正在测试只读连接…';
  try {
    const response = await fetch('/api/onboarding/' + action, {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload),
    });
    if (!response.ok) throw new Error('Cannot complete onboarding');
    const result = await response.json();
    if (result.saved) {
      form.reset();
      selector.value = payload.provider;
      await loadOnboarding();
      get('onboarding-workbench').hidden = false;
      window.dispatchEvent(new Event('stackorion:onboarding-saved'));
    }
    const usable = result.required_scope ? renderResult(result) : false;
    if (!result.required_scope) details.hidden = true;
    state.textContent = result.saved ? '只读服务已接入。' : labels[result.status];
    save.disabled = action !== 'test' || !usable;
  } catch {
    state.textContent = '无法完成接入。请核对输入格式或稍后重试。';
    details.hidden = true;
  } finally {
    busy = test.disabled = selector.disabled = false;
    for (const input of fields) input.disabled = false;
  }
}

selector.addEventListener('change', () => {
  clearSecrets();
  renderProvider();
});
form.addEventListener('input', () => {
  save.disabled = true;
  details.hidden = true;
  if (!busy) state.textContent = '输入已更新，请重新测试只读连接。';
});
form.addEventListener('submit', event => { event.preventDefault(); submit('test'); });
save.addEventListener('click', () => submit('save'));
const onboardingReady = loadOnboarding();

export async function focusOnboardingAccount() {
  const hash = location.hash;
  const [section, query] = hash.split('?');
  if (section !== '#settings') return;
  const account = new URLSearchParams(query).get('account');
  if (!account) return;
  const previousFocus = document.activeElement;
  await onboardingReady;
  if (location.hash !== hash || document.activeElement !== previousFocus) return;
  const matches = [...get('onboarding-connection-list').children]
    .filter(row => row.dataset.inspectionAccount === account);
  const target = matches.length === 1 ? matches[0].querySelector('strong') : get('settings');
  target.focus();
}
