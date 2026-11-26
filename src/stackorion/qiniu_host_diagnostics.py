"""Fixed metadata diagnostics through an owned host helper."""
import json
import os
import re
import subprocess
import sys
from .catalog import CatalogError
from .qiniu import _regular_owned
from .qiniu_host import QiniuHostCapacityAdapter

class QiniuHostDiagnosticsAdapter(QiniuHostCapacityAdapter):
    def inspect(self,alias):return self.journal(alias,'docker.service')

    def journal(self,alias,unit,hours=1,limit=100):
        if unit not in {'docker.service','1panel.service','fail2ban.service','ssh.service','sshd.service'} or type(hours) is not int or not 1<=hours<=24 or type(limit) is not int or not 1<=limit<=500:
            raise CatalogError('invalid bounded fixed-unit journal query')
        return self._query(alias,'journal',unit,hours,limit)

    def container(self,alias,container_id):
        if not isinstance(container_id,str) or not re.fullmatch(r'[0-9a-f]{64}',container_id):
            raise CatalogError('exact full container ID required')
        return self._query(alias,'container',container_id,1,100)

    def _query(self,alias,report,target,hours,limit):
        if alias not in self.targets:raise CatalogError('unknown host target')
        _regular_owned(self.script,secret=False);_regular_owned(self.connection,secret=False)
        if self.connection.stat().st_mode&0o077:raise CatalogError('connection file must be private')
        env={k:os.environ[k] for k in ('HOME','PATH','LANG','SSH_AUTH_SOCK') if k in os.environ}
        try:
            proc=subprocess.run([sys.executable,str(self.script),'--connection-file',str(self.connection),
              '--instance-id',self.targets[alias],'--report',report,'--target',target,'--hours',str(hours),'--limit',str(limit)],
              env=env,capture_output=True,text=True,timeout=65)
            if proc.returncode or len(proc.stdout)>262144:raise ValueError()
            raw=json.loads(proc.stdout)
            if (raw.get('schema')!='qiniu.host-diagnostics.v1' or raw.get('instance_id')!=self.targets[alias] or
                    raw.get('report')!=report or raw.get('target')!=target or raw.get('mutation_invoked') is not False):raise ValueError()
            data=raw['data']
            if not isinstance(data,dict):raise ValueError()
            if report=='journal':
                counts=data['priority_counts'];count=data['sampled_count']
                if (type(count) is not int or not 0<=count<=limit or not isinstance(counts,dict) or
                        set(counts)!={str(i) for i in range(8)} or any(type(v) is not int or v<0 for v in counts.values()) or
                        type(data.get('unknown_priority')) is not int or data['unknown_priority']<0 or
                        sum(counts.values())+data['unknown_priority']!=count):raise ValueError()
                clean={k:data[k] for k in ('sampled_count','priority_counts','unknown_priority','first_timestamp_us','last_timestamp_us','coverage') if k in data}
                clean.update(hours=hours,line_limit=limit)
            else:
                if data.get('id')!=target or type(data.get('running')) is not bool or type(data.get('oom_killed')) is not bool or type(data.get('exit_code')) is not int or type(data.get('restart_count')) is not int:raise ValueError()
                clean={k:data[k] for k in ('id','running','status','exit_code','oom_killed','started_at','finished_at','restart_count','health') if k in data}
            return {'status':'observed','provider':'qiniu','target':alias,'report':report,'data':clean,'mutation_invoked':False,'observed_at':raw.get('observed_at')}
        except (OSError,subprocess.TimeoutExpired,ValueError,KeyError,TypeError,AttributeError) as exc:
            raise CatalogError('host diagnostics unavailable or identity mismatch') from exc
