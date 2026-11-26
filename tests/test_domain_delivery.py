import unittest
from unittest.mock import patch
from stackorion.catalog import Catalog,CatalogError,Service
from stackorion.domain_delivery import inspect_delivery

class DomainDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.catalog=Catalog([Service('cf','python-adapter','cloudflare',True,{},'cloudflare-domains'),Service('ss','python-adapter','spaceship',True,{},'spaceship-management')])
    def test_owned_query_precedes_observation(self):
        with patch('stackorion.domain_delivery.QueryRegistry.query',return_value={'zone':{'name':'example.com','id':'z','name_servers':['ns.example.net']}}),patch('stackorion.domain_delivery.observe',return_value={}) as observation:
            inspect_delivery(self.catalog,'cf','example.com','z');observation.assert_called_once_with('example.com',['ns.example.net'])
    def test_wrong_or_unavailable_owner_stops_public_probe(self):
        with patch('stackorion.domain_delivery.QueryRegistry.query',return_value={'zone':{'name':'wrong.example','id':'z'}}),patch('stackorion.domain_delivery.observe') as observation:
            with self.assertRaises(CatalogError):inspect_delivery(self.catalog,'cf','example.com','z')
            observation.assert_not_called()
        with patch('stackorion.domain_delivery.observe') as observation:
            with self.assertRaises(CatalogError):inspect_delivery(self.catalog,'ss','example.com','z')
            observation.assert_not_called()
