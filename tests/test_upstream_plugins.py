import unittest
from unittest.mock import Mock, patch

from stackorion.plugin_api import Catalog, CatalogError, Service
from stackorion.queries import QueryRegistry


class OutsideAdapter:
    provider = "outside"
    query_methods = {"inventory": "discover"}
    query_upstreams = {"inventory": "official-mcp"}

    def __init__(self, settings):
        self.target = settings["target"]

    def discover(self, limit: int = 1):
        return {"status": "observed", "items": [self.target][:limit]}

    def internal(self):
        raise AssertionError("unregistered method was reached")


class UpstreamPluginTests(unittest.TestCase):
    def service(self, provider="outside", enabled=True):
        return Service("outside-main", "python-adapter", provider, enabled,
                       {"target": "demo"}, "outside-inventory")

    def entry(self, adapter=OutsideAdapter):
        entry = Mock()
        entry.name = "outside-inventory"
        entry.load.return_value = adapter
        return entry

    def test_third_party_query_declares_its_own_upstream_and_signature(self):
        service = self.service()
        registry = QueryRegistry(Catalog([service]))
        with patch("stackorion.catalog.entry_points", return_value=[self.entry()]):
            entry = registry.describe(service.id)["queries"][0]
            self.assertEqual(entry, {"id": "inventory", "mutates": False, "upstream": "official-mcp",
                                     "parameters": [{"name": "limit", "required": False,
                                                     "python_type": "int", "default": 1}]})
            self.assertEqual(registry.query(service.id, "inventory", {"limit": 1})["items"], ["demo"])
            with self.assertRaisesRegex(CatalogError, "not registered"):
                registry.query(service.id, "internal", {})
            with self.assertRaisesRegex(CatalogError, "signature"):
                registry.query(service.id, "inventory", {"wrong": 1})

    def test_invalid_upstream_metadata_is_rejected(self):
        class InvalidAdapter(OutsideAdapter):
            query_upstreams = {"inventory": "https://sensitive.example/"}

        service = self.service()
        with patch("stackorion.catalog.entry_points", return_value=[self.entry(InvalidAdapter)]):
            with self.assertRaisesRegex(CatalogError, "^invalid registered query upstream$"):
                QueryRegistry(Catalog([service])).describe(service.id)

    def test_disabled_ambiguous_and_mismatched_plugins_do_not_construct(self):
        constructor = Mock()
        constructor.provider = "outside"
        service = self.service(provider="wrong")
        entry = self.entry(constructor)
        with patch("stackorion.catalog.entry_points", return_value=[entry]):
            with self.assertRaisesRegex(CatalogError, "provider"):
                Catalog([service]).adapter(service)
        constructor.assert_not_called()
        entry.load.reset_mock()
        disabled = self.service(enabled=False)
        with patch("stackorion.catalog.entry_points") as points:
            with self.assertRaisesRegex(CatalogError, "disabled"):
                Catalog([disabled]).adapter(disabled)
            points.assert_not_called()
        enabled = self.service()
        for entries in ([], [entry, entry]):
            with self.subTest(count=len(entries)), patch("stackorion.catalog.entry_points", return_value=entries):
                with self.assertRaisesRegex(CatalogError, "missing or ambiguous"):
                    Catalog([enabled]).adapter(enabled)
        entry.load.assert_not_called()

    def test_load_failure_is_reported_without_internal_details(self):
        service = self.service()
        entry = self.entry()
        entry.load.side_effect = ImportError("local module and credential details")
        with patch("stackorion.catalog.entry_points", return_value=[entry]):
            with self.assertRaisesRegex(CatalogError, "^adapter could not be loaded$"):
                Catalog([service]).adapter(service)
