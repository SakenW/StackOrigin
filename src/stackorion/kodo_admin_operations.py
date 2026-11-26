"""Exact-object Kodo admin plans and non-replayable journaled dispatch."""

import hashlib
import json
import re
from uuid import UUID

from .catalog import CatalogError
from .kodo_admin import KodoAdminAdapter, OPERATIONS, check_change
from .operation_runtime import load_journal, read_receipt, resource_lock, state_root
from .operations import OperationDefinition


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def operation_uuid(value):
    try:
        parsed = UUID(value)
        if str(parsed) != value or parsed.version != 4:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise CatalogError('operation_id must be a canonical lowercase UUID v4') from None
    return value


class KodoAdminOperations:
    provider = 'qiniu'
    definitions = (
        OperationDefinition('kodo.change-mime.v1', 'plan_kodo_mime_change', 'apply_kodo_mime_change', 'get_kodo_admin_operation'),
        OperationDefinition('kodo.change-storage-class.v1', 'plan_kodo_storage_class_change', 'apply_kodo_storage_class_change', 'get_kodo_admin_operation'),
        OperationDefinition('kodo.restore-archive.v1', 'plan_kodo_archive_restore', 'apply_kodo_archive_restore', 'get_kodo_admin_operation'),
        OperationDefinition('kodo.delete-after-days.v1', 'plan_kodo_delete_after_days', 'apply_kodo_delete_after_days', 'get_kodo_admin_operation'),
        OperationDefinition('kodo.change-status.v1', 'plan_kodo_status_change', 'apply_kodo_status_change', 'get_kodo_admin_operation'),
    )

    def __init__(self, catalog):
        self.catalog = catalog

    def adapter(self, service_id):
        service = self.catalog.get(service_id)
        if (not service.enabled or service.kind != 'python-adapter' or service.provider != 'qiniu'
                or service.adapter != 'qiniu-kodo-admin' or not service.account_id):
            raise CatalogError('enabled qiniu-kodo-admin service with shared account_id is required')
        adapter = self.catalog.adapter(service)
        if not isinstance(adapter, KodoAdminAdapter):
            raise CatalogError('unexpected Kodo admin adapter')
        return service, adapter

    def _plan(self, service_id, operation, alias, key, parameters):
        check_change(operation, key, parameters)
        service, adapter = self.adapter(service_id)
        if not isinstance(alias, str) or alias not in adapter.targets:
            raise CatalogError('unknown Kodo admin target')
        target, client, _, fingerprint, endpoints = adapter.context(alias)
        old = adapter.stat_with_client(client, target['bucket'], key)
        if old is None:
            raise CatalogError('exact object does not exist')
        if operation in {'kodo.change-mime.v1', 'kodo.change-storage-class.v1'}:
            if old['type'] in (2, 3) and old['restoreStatus'] != 2:
                raise CatalogError('archived object must be fully restored before this change')
        if operation == 'kodo.restore-archive.v1' and (old['type'] not in (2, 3) or old['restoreStatus'] != 0):
            raise CatalogError('restore requires a frozen archive; extension or duplicate restore is blocked')
        field = {'kodo.change-mime.v1': 'mimeType', 'kodo.change-storage-class.v1': 'type',
                 'kodo.change-status.v1': 'status'}.get(operation)
        if field and old[field] == parameters[OPERATIONS[operation]]:
            raise CatalogError('object already has the requested value; no change is needed')
        basis = {'operation': operation, 'service': service_id, 'account_id': service.account_id,
                 'target': alias, 'bucket': target['bucket'], 'key': key, 'parameters': parameters,
                 'foundation_sha256': fingerprint, 'endpoints': endpoints, 'remote': old,
                 'contract': 'kodo-admin.v1'}
        return {**basis, 'status': 'planned', 'state_digest': digest(basis), 'mutation_invoked': False,
                'impact': {'object_count': 1, 'provider_condition': ['hash', 'fsize', 'putTime']
                           if operation == 'kodo.change-mime.v1' else [],
                           'external_writer_race': True,
                           'condition_covers_all_old_metadata': False,
                           'automatic_rollback': False,
                           'expiration_deletion_irreversible': operation == 'kodo.delete-after-days.v1',
                           'may_incur_storage_or_restore_charges': operation in {
                               'kodo.change-storage-class.v1', 'kodo.restore-archive.v1'},
                           'readback_limit': 'stat cannot prove restore duration or exact delete-after-days; '
                                            'legacy expiration may be absent'},
                'authorization': 'approve exact bucket, key, old metadata, parameters and state_digest'}, (adapter, client)

    def plan(self, service_id, operation, alias, key, **parameters):
        return self._plan(service_id, operation, alias, key, parameters)[0]

    @staticmethod
    def _observation(plan, after, accepted):
        old = plan['remote']
        same = after is not None and all(after.get(k) == old[k] for k in ('hash', 'fsize', 'putTime'))
        operation, parameters = plan['operation'], plan['parameters']
        field = {'kodo.change-mime.v1': 'mimeType', 'kodo.change-storage-class.v1': 'type',
                 'kodo.change-status.v1': 'status'}.get(operation)
        matches = same and field is not None and after.get(field) == parameters[OPERATIONS[operation]]
        # Check unrelated policy fields too. lastModify and restoreStatus may legitimately
        # change on storage-class transitions; never use lastModify as object identity.
        if matches:
            stable = {'mimeType', 'type', 'status', 'expiration'} - {field}
            matches = all(after.get(k) == old.get(k) for k in stable)
        if matches:
            status, verification = 'success', 'requested field and original object identity observed'
        elif same and accepted and operation == 'kodo.restore-archive.v1' and after['type'] == old['type']:
            status, verification = 'accepted', 'restore request accepted; completion and duration are not certified'
        elif same and accepted and operation == 'kodo.delete-after-days.v1':
            status, verification = 'accepted-unverified', 'request accepted; exact expiration policy is not certified by stat'
        else:
            status, verification = 'needs-reconciliation', 'outcome cannot be established; never redispatch this intent'
        return {'status': status, 'object_identity_matches': same, 'object': after,
                'verification': verification, 'policy_verified': status == 'success'}

    def apply(self, service_id, operation, alias, key, expected_state, confirm_bucket,
              confirm_key, operation_id, **parameters):
        check_change(operation, key, parameters)
        operation_uuid(operation_id)
        if not isinstance(expected_state, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_state):
            raise CatalogError('approved state digest is required')
        service, adapter = self.adapter(service_id)
        if (not isinstance(alias, str) or alias not in adapter.targets
                or confirm_bucket != adapter.targets[alias]['bucket'] or confirm_key != key):
            raise CatalogError('exact configured bucket and key confirmation is required')
        _, fingerprint = adapter.foundation()
        identity = {'operation': operation, 'service': service_id, 'account_id': service.account_id,
                    'bucket': confirm_bucket, 'target': alias, 'key': key, 'parameters': parameters,
                    'expected_state': expected_state, 'foundation_sha256': fingerprint}
        journal = load_journal(adapter.foundation_root / 'scripts/foundation_operation.py')
        root = state_root()
        # Exactly the lock identity used by existing KodoOperations.
        resource = json.dumps([service.account_id, confirm_bucket, key], separators=(',', ':'))
        with resource_lock(root, resource):
            try:
                with journal.Operation(root, operation_id, identity, resume=True) as job:
                    reused = job.value['stages'].get('change', {}).get('status') == 'success'

                    def execute():
                        plan, (current_adapter, client) = self._plan(service_id, operation, alias, key, parameters)
                        if plan['state_digest'] != expected_state:
                            raise CatalogError('approved Kodo state changed; no mutation invoked')
                        # Persist approved old metadata before the one permitted dispatch.
                        job.stage('approved-state', lambda: plan)
                        accepted = False
                        try:
                            current_adapter.change_object(client, confirm_bucket, key, operation, parameters, plan['remote'])
                            accepted = True
                        except Exception:
                            pass
                        try:
                            after = current_adapter.stat_with_client(client, confirm_bucket, key)
                            observation = self._observation(plan, after, accepted)
                        except Exception:
                            observation = {'status': 'needs-reconciliation', 'policy_verified': False,
                                           'verification': 'readback failed; never replay unknown dispatch'}
                        return {**observation, 'operation': operation, 'bucket': confirm_bucket, 'key': key,
                                'mutation_invoked': True, 'dispatch_outcome': 'accepted' if accepted else 'unknown',
                                'replayed': False}

                    result = job.stage('change', execute)
                    if result['status'] == 'success':
                        job.complete()
                    else:
                        # A completed dispatch stage is cached even if its business outcome
                        # is pending. It must never be refreshed or marked retry_safe.
                        job.value['status'] = result['status']
                        job.save()
                    return {**result, 'operation_id': operation_id, 'reused_completed_receipt': reused,
                            'live_state_verified_this_invocation': not reused and 'object' in result}
            except Exception:
                return {'status': 'needs-reconciliation', 'operation_id': operation_id, 'replayed': False,
                        'reason': 'preflight, journal or execution interrupted; inspect receipt without replay'}

    def receipt(self, operation_id):
        operation_uuid(operation_id)
        return read_receipt(operation_id, set(OPERATIONS))

    def reconcile(self, service_id, operation_id):
        """Read current metadata only; never retry or rewrite the original receipt."""
        receipt = self.receipt(operation_id)
        identity = receipt['identity']
        if identity.get('service') != service_id:
            raise CatalogError('receipt belongs to another service')
        service, adapter = self.adapter(service_id)
        if identity.get('account_id') != service.account_id:
            raise CatalogError('receipt account changed')
        alias, key = identity['target'], identity['key']
        if alias not in adapter.targets or adapter.targets[alias]['bucket'] != identity['bucket']:
            raise CatalogError('receipt target changed')
        root = state_root(create=False)
        resource = json.dumps([service.account_id, identity['bucket'], key], separators=(',', ':'))
        with resource_lock(root, resource):
            receipt = self.receipt(operation_id)
            plan = receipt['stages'].get('approved-state', {}).get('result')
            if not isinstance(plan, dict) or plan.get('state_digest') != identity['expected_state']:
                raise CatalogError('no approved dispatched state is available for reconciliation')
            target, client, *_ = adapter.context(alias)
            after = adapter.stat_with_client(client, target['bucket'], key)
            result = receipt['stages'].get('change', {}).get('result') or {}
            observation = self._observation(plan, after, result.get('dispatch_outcome') == 'accepted')
            return {**observation, 'operation_id': operation_id, 'mode': 'read-only-reconciliation',
                    'receipt_status': receipt['status'], 'mutation_invoked': False, 'replayed': False,
                    'attribution': 'current state observation does not prove which writer caused it'}
