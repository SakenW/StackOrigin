import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from stackorion.catalog import CatalogError
from stackorion.qiniu_host_diagnostics import QiniuHostDiagnosticsAdapter

class DiagnosticsTests(unittest.TestCase):
    def test_exact_validation_and_output_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);script=root/'helper.py';script.write_text('owned');env=root/'conn';env.write_text('private');env.chmod(0o600)
            adapter=QiniuHostDiagnosticsAdapter({'script_path':str(script),'connection_file':str(env),'targets':[{'alias':'prod','instance_id':'i-one'}]})
            data={'schema':'qiniu.host-diagnostics.v1','instance_id':'i-one','report':'journal','target':'docker.service','mutation_invoked':False,
                  'data':{'sampled_count':1,'priority_counts':{str(i):int(i==3) for i in range(8)},'unknown_priority':0,'MESSAGE':'SECRET'}}
            with patch('stackorion.qiniu_host_diagnostics.subprocess.run',return_value=subprocess.CompletedProcess([],0,json.dumps(data),'')):
                self.assertNotIn('SECRET',str(adapter.journal('prod','docker.service')))
            for unit in ['app.service','--all','docker.service;id']:
                with self.assertRaises(CatalogError):adapter.journal('prod',unit)
            with self.assertRaises(CatalogError):adapter.journal('prod','docker.service',limit=501)
            with self.assertRaises(CatalogError):adapter.container('prod','--all')
            with patch('stackorion.qiniu_host_diagnostics.subprocess.run',return_value=subprocess.CompletedProcess([],0,json.dumps({**data,'instance_id':'other'}),'')):
                with self.assertRaises(CatalogError):adapter.journal('prod','docker.service')
