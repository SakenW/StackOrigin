"""Fixed host service and backup metadata queries through the owned helper."""

from datetime import datetime, timezone
import json
import os
import subprocess
import sys

from .catalog import CatalogError
from .qiniu import _regular_owned
from .qiniu_host import QiniuHostCapacityAdapter


class QiniuHostStatusAdapter(QiniuHostCapacityAdapter):
    def inspect(self, alias):
        return self.services(alias)

    def services(self, alias):
        return self._query(alias, 'services')

    def backups(self, alias):
        return self._query(alias, 'backups')

    def _query(self, alias, report):
        if alias not in self.targets or report not in {'services', 'backups'}:
            raise CatalogError('unknown host observation target or report')
        _regular_owned(self.script, secret=False)
        _regular_owned(self.connection, secret=False)
        if self.connection.stat().st_mode & 0o077:
            raise CatalogError('host connection file must be private (0600)')
        env = {k: os.environ[k] for k in ('HOME', 'PATH', 'LANG', 'SSH_AUTH_SOCK') if k in os.environ}
        try:
            proc = subprocess.run([sys.executable, str(self.script), '--connection-file', str(self.connection),
                          '--instance-id', self.targets[alias], '--report', report],
                          env=env, capture_output=True, text=True, timeout=65)
            if proc.returncode or len(proc.stdout) > 262144:
                raise ValueError()
            payload = json.loads(proc.stdout)
            if (payload.get('schema') != 'qiniu.host-status.v1' or payload.get('instance_id') != self.targets[alias]
                    or payload.get('report') != report or payload.get('mutation_invoked') is not False):
                raise ValueError()
            data = payload['data']
            if report == 'services':
                units = data['units']; docker = data['docker']
                if not isinstance(units, list) or len(units) > 5 or not isinstance(docker, dict):
                    raise ValueError()
                allowed = {'docker.service', '1panel.service', 'fail2ban.service', 'ssh.service', 'sshd.service'}
                if any(not isinstance(u, dict) or u.get('unit') not in allowed for u in units):
                    raise ValueError()
                clean = {'units': [{k: u[k] for k in ('unit', 'load_state', 'active_state', 'sub_state') if k in u}
                                   for u in units], 'docker': {k: docker[k] for k in ('status', 'coverage', 'reason') if k in docker}}
                if docker.get('status') == 'observed':
                    rows = docker['containers']
                    if (not isinstance(rows, list) or len(rows) > 50 or
                            any(not isinstance(r, dict) or any(not isinstance(r.get(k), str) for k in ('id', 'name', 'state', 'status')) for r in rows)):
                        raise ValueError()
                    clean['docker'].update(returned_count=len(rows), containers=[{k: r[k] for k in ('id', 'name', 'state', 'status')} for r in rows])
            else:
                if data.get('status') not in {'observed', 'incomplete', 'missing', 'unavailable'}:
                    raise ValueError()
                clean = {k: data[k] for k in ('status', 'timestamp', 'completed_at_utc', 'age_hours', 'fresh', 'coverage', 'reason') if k in data}
                if 'artifacts' in data:
                    clean['artifacts'] = {kind: {k: data['artifacts'][kind][k] for k in ('declared_count', 'present_nonempty_count')}
                                          for kind in ('postgres', 'archives')}
            return {'provider': 'qiniu', 'target': alias, 'instance_id': self.targets[alias], 'report': report,
                    'status': 'observed', 'observed_at': payload.get('observed_at', datetime.now(timezone.utc).isoformat()),
                    'mutation_invoked': False, 'data': clean}
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise CatalogError('host observation unavailable or identity contract mismatch') from exc
