import unittest
from unittest.mock import patch
from stackorion.catalog import CatalogError
from stackorion.qiniu_las import QiniuLASAdapter


class LASAdapterTests(unittest.TestCase):
    def adapter(self):
        return QiniuLASAdapter({'script_path': '/server/qiniu_las_tool.py',
                                'credential_file': '/private/qiniu.env',
                                'targets': [{'alias': 'asof-preview', 'region': 'ap-southeast-4',
                                             'instance_id': 'i-ab'}]})

    def payload(self):
        return {'product': 'las', 'status': 'observed', 'region': 'ap-southeast-4',
                'instance': {'instanceID': 'i-ab', 'region': {'id': 'ap-southeast-4'},
                             'password': 'SECRET', 'publicAccess': [{'type': 'PortForward',
                             'endpoints': [{'endpoint': 'host:10043', 'token': 'SECRET'}]}]}}

    def test_exact_read_only_arguments_and_projection(self):
        with patch('stackorion.qiniu_las.run_qiniu_helper', return_value=self.payload()) as run:
            result = self.adapter().inspect('asof-preview')
        self.assertEqual(run.call_args.args[2], ['--region', 'ap-southeast-4', '--instance-id', 'i-ab', 'audit'])
        self.assertNotIn('SECRET', str(result))
        self.assertEqual(result['data']['publicAccess'][0]['endpoints'][0]['endpoint'], 'host:10043')
        self.assertEqual(result['coverage']['host_key'], 'not_checked')

    def test_host_reading_keeps_las_specs_separate_from_usage(self):
        payload = self.payload()
        payload["observed_at"] = "2026-11-23T00:00:00+00:00"
        payload["instance"].update(state="Running", instanceTypeInfo={"cpu": 4, "memory": 8192},
                                   systemDisk={"size": 100}, internetMaxBandwidth=50)
        adapter = self.adapter()
        with patch("stackorion.qiniu_las.run_qiniu_helper", return_value=payload) as run:
            self.assertEqual(adapter.host_target("asof-preview").product, "las")
            run.assert_not_called()
            reading = adapter.host_reading("asof-preview")
        self.assertEqual(reading.state, "running")
        self.assertEqual(reading.source, "las-api")
        self.assertEqual(reading.values, {})
        self.assertEqual(run.call_args.args[2][-1], "audit")

    def test_unknown_target_never_invokes_helper(self):
        with patch('stackorion.qiniu_las.run_qiniu_helper') as run:
            with self.assertRaises(CatalogError):
                self.adapter().inspect('production')
            run.assert_not_called()

    def test_response_mismatch_is_rejected(self):
        value = self.payload()
        value['instance']['region']['id'] = 'cn-other-1'
        with patch('stackorion.qiniu_las.run_qiniu_helper', return_value=value):
            with self.assertRaises(CatalogError):
                self.adapter().inspect('asof-preview')

    def test_region_and_instance_cannot_inject_paths(self):
        for region, instance in [('evil.example/x', 'i-ab'), ('ap-southeast-4', 'i-ab/stop')]:
            with self.assertRaises(CatalogError):
                QiniuLASAdapter({'script_path': '/helper.py', 'credential_file': '/secret.env',
                                 'targets': [{'alias': 'a', 'region': region, 'instance_id': instance}]})
