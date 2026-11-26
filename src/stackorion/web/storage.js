const byId = (id) => document.getElementById(id);
const retentionInput = byId('retention-days');
const backupInput = byId('backup-key');
const backupButton = byId('backup-download');
const evidenceClock = new Intl.DateTimeFormat('zh-CN', {
  dateStyle:'medium', timeStyle:'short', timeZone:'Asia/Shanghai',
});

function formatBytes(bytes) {
  if (bytes < 1024) return bytes + ' B';
  const power = Math.min(Math.floor(Math.log2(bytes) / 10), 3);
  return (bytes / 1024 ** power).toLocaleString('zh-CN', {maximumFractionDigits:1}) + ' ' +
    ['B', 'KiB', 'MiB', 'GiB'][power];
}

function renderRetention(days) {
  retentionInput.value = days ?? '';
  byId('retention-current').textContent = days === null ? '不限期' : days + ' 天';
}

function notice(id, text, error = false) {
  const target = byId(id);
  target.textContent = text;
  target.dataset.tone = error ? 'high' : 'neutral';
}

function renderBackupEvidence(evidence) {
  for (const [id, row] of [['backup-generated', evidence?.generated], ['backup-verified', evidence?.restore_drill]]) {
    byId(id).textContent = row
      ? evidenceClock.format(new Date(row.observed_at)) + ' UTC+8 · ' + row.digest.slice(0, 12)
      : '无记录';
  }
}

async function loadStorage() {
  try {
    const response = await fetch('/api/storage');
    if (!response.ok) throw new Error('Storage unavailable');
    const info = await response.json();
    byId('storage-path').textContent = info.state_dir;
    byId('storage-live-bytes').textContent = formatBytes(info.live.bytes);
    byId('storage-demo-bytes').textContent = info.demo ? formatBytes(info.demo.bytes) : '未启用';
    byId('storage-encryption').textContent = info.live.encrypted ? '已加密' : '未加密';
    renderRetention(info.retention_days);
    renderBackupEvidence(info.backup_evidence);
    backupInput.disabled = backupButton.disabled = !info.backup_available;
    byId('backup-unavailable').hidden = info.backup_available;
    byId('storage-details').hidden = false;
    notice('storage-load-status', '');
  } catch {
    notice('storage-load-status', '无法读取存储状态，请刷新页面重试。', true);
  }
}

async function postStorage(action, payload) {
  const response = await fetch('/api/storage/' + action, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(payload),
  });
  if (response.ok) return response;
  const result = await response.json().catch(() => ({}));
  throw new Error(result.error || 'storage_unavailable');
}

byId('retention-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const days = retentionInput.value === '' ? null : retentionInput.valueAsNumber;
  if (days !== null && !window.confirm('保存后将立即删除超过 ' + days + ' 天的真实采集样本。确定继续？')) return;
  const button = byId('retention-save');
  button.disabled = true;
  notice('retention-status', '正在保存…');
  try {
    const response = await postStorage('retention', {days});
    const {retention_days, removed} = await response.json();
    renderRetention(retention_days);
    notice('retention-status', removed ? '已保存，清理了 ' + removed + ' 条旧样本。' : '已保存。');
  } catch {
    notice('retention-status', '保留期保存失败，请重试。', true);
  } finally {
    button.disabled = false;
  }
});

const backupErrors = {
  invalid_recipient:'请输入完整的 age1… 公钥。',
  backup_unavailable:'当前环境无法创建加密备份。',
  backup_too_large:'状态库超过 64 MiB 或配置文件过大，无法从面板导出。',
  backup_busy:'已有备份正在生成，请稍后重试。',
  encryption_failed:'加密失败，请检查公钥和 age 安装后重试。',
};

byId('backup-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  backupButton.disabled = true;
  notice('backup-status', '正在加密…');
  try {
    const response = await postStorage('backup', {recipient:backupInput.value});
    const url = URL.createObjectURL(await response.blob());
    const download = document.createElement('a');
    download.href = url;
    download.download = /filename="([^"]+)"/.exec(response.headers.get('Content-Disposition') || '')?.[1]
      || 'stackorion.tar.gz.age';
    document.body.append(download);
    download.click();
    download.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    notice('backup-status', '已生成加密备份。');
    await loadStorage();
  } catch (error) {
    notice('backup-status', backupErrors[error.message] || '备份失败，请重试。', true);
  } finally {
    backupButton.disabled = false;
  }
});

loadStorage();
