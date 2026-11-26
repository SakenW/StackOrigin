import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from stackorion.catalog import CatalogError
from stackorion.qiniu_host_status import QiniuHostStatusAdapter


class HostStatusAdapterTests(unittest.TestCase):
    def test_service_filter_and_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);script=root/'helper.py';script.write_text('owned')
            env=root/'connection';env.write_text('test');env.chmod(0o600)
            adapter=QiniuHostStatusAdapter({'script_path':str(script),'connection_file':str(env),
                                          'targets':[{'alias':'prod','instance_id':'i-one'}]})
            data={'schema':'qiniu.host-status.v1','instance_id':'i-one','report':'services','mutation_invoked':False,
                  'data':{'units':[{'unit':'docker.service','active_state':'active','secret':'hidden'}],
                          'docker':{'status':'observed','containers':[{'id':'id','name':'app','state':'running','status':'Up','env':'hidden'}]}}}
            def response(payload): return subprocess.CompletedProcess([],0,json.dumps(payload),'')
            with patch('stackorion.qiniu_host_status.subprocess.run',return_value=response(data)) as run:
                self.assertNotIn('hidden',str(adapter.services('prod')))
                self.assertEqual(run.call_args.args[0][-2:],['--report','services'])
            for field,value in [('instance_id','i-other'),('mutation_invoked',True),('report','backups')]:
                with patch('stackorion.qiniu_host_status.subprocess.run',return_value=response({**data,field:value})):
                    with self.assertRaises(CatalogError):adapter.services('prod')
            with self.assertRaises(CatalogError):adapter.services('unknown')
            env.chmod(0o644)
            with self.assertRaises(CatalogError):adapter.services('prod')

    def test_backup_filter_and_unavailable_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);script=root/'helper.py';script.write_text('owned');env=root/'connection';env.write_text('test');env.chmod(0o600)
            adapter=QiniuHostStatusAdapter({'script_path':str(script),'connection_file':str(env),'targets':[{'alias':'prod','instance_id':'i-one'}]})
            data={'schema':'qiniu.host-status.v1','instance_id':'i-one','report':'backups','mutation_invoked':False,
                  'data':{'status':'missing','secret':'hidden'}}
            with patch('stackorion.qiniu_host_status.subprocess.run',return_value=subprocess.CompletedProcess([],0,json.dumps(data),'')):
                result=adapter.backups('prod');self.assertEqual(result['data']['status'],'missing');self.assertNotIn('hidden',str(result))
