import subprocess
import unittest
from unittest.mock import patch, MagicMock
from stackorion.catalog import CatalogError
from stackorion.domain_observation import dig,https_head,observe

class DomainObservationTests(unittest.TestCase):
    def test_slow_fragmented_headers_obey_absolute_deadline(self):
        raw=MagicMock(); raw.__enter__.return_value=raw
        connection=MagicMock(); connection.__enter__.return_value=connection
        connection.recv.return_value=b'H'
        context=MagicMock(); context.wrap_socket.return_value=connection
        with patch('stackorion.domain_observation.socket.create_connection',return_value=raw), \
                patch('stackorion.domain_observation.ssl.create_default_context',return_value=context), \
                patch('stackorion.domain_observation.time.monotonic',side_effect=[0,0,1,2,3,7,11]):
            result=https_head('example.com','1.1.1.1')
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(connection.recv.call_count,2)
        self.assertEqual(connection.settimeout.call_args.args,(3,))
    def test_private_endpoints_and_invalid_domains_never_connect(self):
        for ip in ['127.0.0.1','10.0.0.1','169.254.169.254','::1']:
            with patch('stackorion.domain_observation.socket.create_connection') as connection:
                with self.assertRaises(CatalogError):https_head('example.com',ip)
                connection.assert_not_called()
        for domain in ['example.com;id','-x.example.com','https://example.com','*.example.com']:
            with self.assertRaises(CatalogError):observe(domain,[])
    def test_authority_flag_and_metadata_only(self):
        text=';; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 1\n;; flags: qr aa; QUERY: 1\nexample.com. 300 IN SOA ns.example.com. hostmaster.example.com. 123 1 2 3 4\n'
        with patch('stackorion.domain_observation.subprocess.run',return_value=subprocess.CompletedProcess([],0,text,'')):
            value=dig('example.com','SOA','1.1.1.1');self.assertTrue(value['authoritative']);self.assertEqual(value['values'][0]['serial'],'123')
    def test_separate_provider_dns_and_https_outcomes(self):
        def lookup(name,kind,server=None):
            if kind=='NS':return {'values':['ns.example.net'],'authoritative':False,'rcode':'NOERROR'}
            if kind=='A':return {'values':['1.1.1.1'],'authoritative':False,'rcode':'NOERROR'}
            if kind=='DS':return {'values':[],'authoritative':False,'rcode':'NOERROR'}
            return {'values':[{'owner':'example.com','serial':'123'}],'authoritative':True,'rcode':'NOERROR'}
        with patch('stackorion.domain_observation.dig',side_effect=lookup),patch('stackorion.domain_observation.https_head',return_value={'status':'unavailable'}):
            result=observe('example.com',['other.example.net'])
            self.assertFalse(result['recursive_ns']['matches_provider']);self.assertTrue(result['authority']['authoritative']);self.assertEqual(result['https']['status'],'unavailable')

    def test_authority_and_ds_failures_do_not_prevent_https_observation(self):
        def lookup(name, kind, server=None):
            if kind == 'NS':
                return {'values': ['ns.example.net'], 'rcode': 'NOERROR'}
            if kind == 'A':
                return {'values': ['1.1.1.1'], 'rcode': 'NOERROR'}
            raise CatalogError('query unavailable')

        with patch('stackorion.domain_observation.dig', side_effect=lookup), \
                patch('stackorion.domain_observation.https_head', return_value={'status': 'observed'}) as https:
            result = observe('example.com', ['ns.example.net'])
        https.assert_called_once_with('example.com', '1.1.1.1')
        self.assertTrue(result['recursive_ns']['matches_provider'])
        self.assertEqual(result['authority']['status'], 'unavailable')
        self.assertNotIn('parent_ds_observation', result)
        self.assertEqual(result['https']['status'], 'observed')
