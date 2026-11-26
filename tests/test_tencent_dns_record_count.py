"""DNSPod metadata count changes must not hide unrelated readback drift."""
import copy
import unittest
from stackorion.tencent_management_operations import _recovery_contract, _verify_recovery


def row(rid, name, value):
    return {'RecordId': rid, 'Name': name, 'Value': value, 'Type': 'A', 'TTL': 600,
            'Status': 'ENABLE', 'Line': '默认', 'LineId': '0', 'Weight': 0,
            'MonitorStatus': '', 'Remark': '', 'DefaultNS': False, 'MX': 0}


class RecordCountTests(unittest.TestCase):
    def states(self, action):
        params = {'action': action, 'resource_id': '200', 'record_id': None if action == 'dns-create' else 101,
                  'name': 'new', 'value': '192.0.2.2', 'record_type': 'A', 'ttl': 600, 'mx': None, 'line_id': '0'}
        old = row(100, 'old', '192.0.2.1')
        before = {'target': {'DomainId': 200, 'RecordCount': 1, 'UpdatedOn': 'old'},
                  'ttl_policy': {'minimum': 600}, 'records': [old]}
        new = row(101, 'new', '192.0.2.2')
        after = copy.deepcopy(before)
        if action == 'dns-create':
            after['records'].append(new); after['target']['RecordCount'] = 2
        elif action == 'dns-delete':
            before['records'].append(new); before['target']['RecordCount'] = 2
        else:
            before['records'].append(row(101, 'new', '192.0.2.3')); before['target']['RecordCount'] = 2
            after['records'].append(new); after['target']['RecordCount'] = 2
        after['target']['UpdatedOn'] = 'new'
        return before, after, params

    def test_exact_create_delete_count_deltas_and_update_no_delta(self):
        for action in ('dns-create', 'dns-delete', 'dns-update'):
            before, after, params = self.states(action)
            self.assertTrue(_verify_recovery(_recovery_contract(before, params), after, params, {'RecordId': 101}), action)

    def test_unrelated_metadata_records_target_policy_and_wrong_delta_stay_fenced(self):
        for action in ('dns-create', 'dns-delete', 'dns-update'):
            before, after, params = self.states(action); contract = _recovery_contract(before, params)
            for mutate in (lambda a: a['target'].update(RecordCount=99),
                           lambda a: a['target'].update(Owner='other'),
                           lambda a: a['records'][0].update(Value='203.0.113.1'),
                           lambda a: a.update(ttl_policy={'minimum': 60})):
                changed = copy.deepcopy(after); mutate(changed)
                self.assertFalse(_verify_recovery(contract, changed, params, {'RecordId': 101}))
            self.assertFalse(_verify_recovery(contract, after, params, {'RecordId': 999}))


    def test_cname_root_dot_is_equivalent_but_other_values_are_not(self):
        before, after, params = self.states('dns-create')
        params.update(record_type='CNAME', value='target.example')
        after['records'][1].update(Type='CNAME', Value='target.example.')
        contract = _recovery_contract(before, params)
        self.assertTrue(_verify_recovery(contract, after, params, {'RecordId': 101}))
        for value in ['other.example.', 'target.example..']:
            after['records'][1]['Value'] = value
            self.assertFalse(_verify_recovery(contract, after, params, {'RecordId': 101}))
        params.update(record_type='TXT', value='text')
        after['records'][1].update(Type='TXT', Value='text.')
        self.assertFalse(_verify_recovery(_recovery_contract(before, params), after, params, {'RecordId': 101}))
