"""Read-only dashboard collection through existing local catalog adapters."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .catalog import Catalog, CatalogError
from .billing_inspection import compare_closed_months
from .domain_observation import HOST
from .inspection import expiry_status, is_public_ssh

PROVIDERS = {
    "tencent": "腾讯云", "qiniu": "七牛云", "spaceship": "Spaceship",
    "cloudflare": "Cloudflare", "aliyun": "阿里云",
}
ADAPTERS = {
    "spaceship-management": ("域名",), "cloudflare-domains": ("域名",),
    "qiniu-qvm": ("实例", "安全组"), "qiniu-las": ("实例",),
    "tencent-cvm": ("实例",), "tencent-lighthouse": ("实例",),
    "tencent-management": ("账务", "安全组", "库存证书"),
    "aliyun-management": ("账务", "实例", "安全组", "域名"),
}

STARTER_ADAPTERS = {**ADAPTERS, "cloudflare-zone": ("域名",), "spaceship-domains": ("域名",)}
DOMAIN_DELIVERY_ADAPTERS = {"cloudflare-domains", "cloudflare-zone", "spaceship-management", "spaceship-domains"}
DOMAIN_EXPIRY_DAYS = 30
CERTIFICATE_EXPIRY_DAYS = 14
TENCENT_CERTIFICATE_TIMEZONE = timezone(timedelta(hours=8))


def load_connections(path):
    return Catalog.load(Path(path)) if path else Catalog([])


def fallback_providers(catalog, *, starter=False):
    """Configured credentials suppress provider-level demo data, even when calls fail."""
    adapters = STARTER_ADAPTERS if starter else ADAPTERS
    return {
        service.provider for row in catalog.list() if (row["enabled"] or starter)
        and (service := catalog.get(row["id"])).adapter in adapters
        and credential_present(service)
    }


def credential_present(service):
    raw = service.settings.get("credential_file")
    return isinstance(raw, str) and Path(raw).is_file()


def inspection_targets(catalog, *, starter=False):
    adapters = STARTER_ADAPTERS if starter else ADAPTERS
    labels = {row["id"]: row["label"] for row in catalog.accounts()}
    for row in catalog.list():
        if not row["enabled"] or row["adapter"] not in adapters:
            continue
        service = catalog.get(row["id"])
        identity = service.account_id or service.id
        key = "live:" + service.provider + ":" + identity
        yield service, key, labels.get(identity, service.id)


def collect(catalog, *, starter=False, account_ids=None):
    adapters = STARTER_ADAPTERS if starter else ADAPTERS
    now = datetime.now(timezone.utc)
    selected = set(account_ids) if account_ids is not None else None
    accounts, jobs = {}, []
    for service, key, label in inspection_targets(catalog, starter=starter):
        if selected is not None and key not in selected:
            continue
        account = accounts.setdefault(key, dict(id=key, label=label,
            provider=PROVIDERS[service.provider], enabled=True, checks={}, complete_scopes=[]))
        checks = adapters[service.adapter]
        if service.adapter == "tencent-management" and service.settings.get("backup_inventory") is True:
            checks += ("云盘备份",)
        if service.adapter == "tencent-management" and service.settings.get("billing_anomaly") is True:
            checks += ("账期费用",)
        for check in checks:
            account["checks"].setdefault(check, [])
            if not credential_present(service):
                account["checks"][check].append("missing_credentials")
                continue
            regions = service.settings.get("regions")
            if check == "账务":
                regions = regions[:1] if regions else [None]
            elif check in {"库存证书", "账期费用"}:
                regions = [None]
            jobs.extend((service, key, check, region) for region in (regions or [None]))

    def read(job):
        service, key, check, region = job
        scope = ":".join((service.id, check, region or "configured"))
        try:
            adapter = catalog.adapter(service)
            status, assets, findings, evidence = read_scope(adapter, service.adapter, check, region, now)
        except (CatalogError, OSError, ValueError, KeyError, TypeError):
            status, assets, findings, evidence = "unavailable", [], [], None
        return service.id, key, check, scope, status, assets, findings, evidence

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(read, jobs))

    assets, findings = [], []
    for service_id, key, check, scope, status, asset_rows, finding_rows, evidence in results:
        account = accounts[key]
        account["checks"][check].append(status)
        if status == "complete":
            account["complete_scopes"].append(scope)
        if evidence is not None:
            if check == "账期费用":
                account.setdefault("billing_checks", []).append({**evidence, "scope": scope})
            else:
                account["balance"] = evidence
        fields = dict(account=key, account_label=account["label"], provider=account["provider"], data_source="live")
        assets.extend({**row, **fields, "scope": scope,
                       **({"service_id": service_id} if "provider_nameservers" in row else {})}
                      for row in asset_rows)
        findings.extend({**row, **fields, "scope": scope} for row in finding_rows)

    coverage = dict.fromkeys(("complete", "partial", "unavailable", "disabled"), 0)
    for key, account in accounts.items():
        checks = account["checks"]
        for name, states in checks.items():
            checks[name] = (
                "complete" if all(state == "complete" for state in states) else
                "partial" if any(state in {"complete", "partial"} for state in states) else
                "missing_credentials" if all(state == "missing_credentials" for state in states) else "unavailable"
            )
        states = checks.values()
        status = ("complete" if all(state == "complete" for state in states) else
                  "partial" if any(state in {"complete", "partial"} for state in states) else "unavailable")
        account["status"] = status
        coverage[status] += 1
        if status != "complete":
            findings.append(dict(kind="coverage", id=key, account=key, account_label=account["label"],
                provider=account["provider"], name=account["label"], data_source="live", status=status, checks=checks,
                reason="部分检查缺少凭据、查询失败或覆盖不完整", last_success=None))

    counts = {kind: sum(row["kind"] == kind for row in findings)
              for kind in ("ssh", "coverage", "expiry", "balance", "billing")}
    report = dict(source="live", scenario="live", observed_at=now.isoformat(), expected_accounts=len(accounts),
                  coverage=coverage, counts=counts, accounts=list(accounts.values()), findings=findings, assets=assets)
    if selected is not None:
        report["selected_accounts"] = account_ids
    return report


def read_scope(adapter, name, check, region, now):
    if check == "账期费用":
        status, evidence, findings = compare_closed_months(adapter, now)
        return status, [], findings, evidence
    if check == "账务":
        if name == "tencent-management":
            result = adapter.balance()
            balance = dict(metric="RealBalance", currency="CNY",
                           amount=str(Decimal(result["amounts"]["RealBalance"]) / 100))
        else:
            result = adapter.balance(region)
            value = result["balance"]
            balance = dict(metric="AvailableAmount", currency=value["Currency"], amount=value["AvailableAmount"])
        return "complete", [], [], balance
    if check == "域名":
        if name == "cloudflare-zone":
            result, field, registrar, dns = adapter.inspect("onboarding-zone"), "name", None, "Cloudflare"
            result = {**result, "data": [result["data"]]}
        elif name == "cloudflare-domains":
            result, field, registrar, dns = adapter.discover(), "name", None, "Cloudflare"
        elif name in {"spaceship-management", "spaceship-domains"}:
            result, field, registrar, dns = adapter.discover(), "name", "Spaceship", None
        else:
            result, field, registrar, dns = adapter.dns_domains(region), "DomainName", None, "阿里云 DNS"
        rows = result.get("items", result.get("data", []))
        status = "complete" if result["status"] == "observed" else result["status"]
        assets, findings = [], []
        for row in rows:
            domain = row[field].lower()
            asset = dict(id=domain, kind="domain", name=domain, registrar=registrar, dns_provider=dns)
            if name in DOMAIN_DELIVERY_ADAPTERS:
                registration = row.get("nameservers")
                servers = registration.get("hosts") if isinstance(registration, dict) else row.get("name_servers")
                asset["provider_nameservers"] = []
                if isinstance(servers, list) and len(servers) <= 12:
                    names = [name.lower().rstrip(".") for name in servers if isinstance(name, str)]
                    if len(names) == len(servers) and all(HOST.fullmatch(name) for name in names):
                        asset["provider_nameservers"] = sorted(set(names))
            if registrar:
                expires = row.get("expirationDate")
                try:
                    expiry = datetime.fromisoformat(expires)
                    if expiry.tzinfo is None:
                        raise ValueError("Registrar timestamp lacks timezone")
                    ends = expiry.isoformat()
                except (TypeError, ValueError):
                    ends = None
                    if status == "complete":
                        status = "partial"
                renewal = row.get("autoRenew")
                asset.update(expires_at=ends, auto_renew=renewal if type(renewal) is bool else None,
                             expiry_status=expiry_status(ends, None, now, DOMAIN_EXPIRY_DAYS))
                if asset["expiry_status"] in {"due", "expired"}:
                    findings.append({**asset, "kind": "expiry", "resource_kind": "domain",
                                     "status": asset["expiry_status"]})
            assets.append(asset)
        return status, assets, findings, None
    if check == "库存证书":
        result = adapter.certificates()
        status = result["status"]
        assets, findings = [], []
        for row in result["items"]:
            expires = row.get("CertEndTime")
            if expires:
                parsed = datetime.fromisoformat(expires)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=TENCENT_CERTIFICATE_TIMEZONE)
                expires = parsed.isoformat()
            elif status == "complete":
                status = "partial"
            cert_id = row["CertificateId"]
            state = expiry_status(expires, None, now, CERTIFICATE_EXPIRY_DAYS)
            asset = dict(id=cert_id, name=row.get("Domain") or cert_id, kind="certificate",
                         expires_at=expires or None, expiry_status=state)
            assets.append(asset)
            if state in {"due", "expired"}:
                findings.append({**asset, "kind": "expiry", "resource_kind": "certificate", "status": state})
        return status, assets, findings, None
    if check == "云盘备份":
        result = adapter.snapshot_backup_status(region)
        return result["status"], [
            {**row, "id": row["policy_id"] + ":" + row["disk_id"], "name": row["disk_id"],
             "region": region, "kind": "backup"}
            for row in result["items"]
        ], [], None
    if check == "实例":
        return inventory(adapter, name, region, now)
    if name == "qiniu-qvm":
        return qiniu_rules(adapter)
    return cloud_rules(adapter, name, region)


def inventory(adapter, name, region, now):
    if name == "qiniu-qvm":
        result = adapter.discover()["data"]["instances"]
        rows, identity, expiry, product = result["data"], "instance_id", "expired_time", "七牛云 QVM"
    elif name == "qiniu-las":
        rows = [adapter.inspect(alias)["data"] for alias in adapter.list_targets()]
        result, identity, expiry, product = {"status": "complete"}, "instanceID", "expiredAt", "七牛云 LAS"
    elif name == "aliyun-management":
        result = adapter.instances(region)
        rows, identity, expiry, product = result["items"], "InstanceId", "ExpiredTime", "阿里云 ECS"
    else:
        result = adapter.inspect(region)
        rows, identity, expiry = result["data"], "InstanceId", "ExpiredTime"
        product = "腾讯云 Lighthouse" if name == "tencent-lighthouse" else "腾讯云 CVM"
    status = "complete" if result["status"] in {"observed", "complete"} else result["status"]
    assets, findings = [], []
    for row in rows:
        ends = row.get(expiry) or None
        billing = str(row.get("instance_charge_type", row.get("InstanceChargeType", ""))).lower()
        billing = ("prepaid" if ends or billing in {"prepaid", "pre_paid"} else
                   "postpaid" if billing in {"postpaid", "post_paid", "postpaid_by_hour"} else "unknown")
        asset = dict(id=row[identity], name=row[identity], kind="server",
                     server_type="lighthouse" if name in {"tencent-lighthouse", "qiniu-las"} else "cloud",
                     product=product, region=region or row.get("region_id") or row.get("region", {}).get("id"),
                     billing=billing, expires_at=None)
        if ends:
            try:
                parsed = datetime.fromisoformat(ends.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError
                asset["expires_at"] = parsed.isoformat()
                expiry_result = expiry_status(asset["expires_at"], billing, now)
                if expiry_result in {"expired", "due"}:
                    findings.append({**asset, "kind": "expiry", "status": expiry_result})
            except (ValueError, AttributeError, OverflowError):
                status = "partial"
        elif billing != "postpaid":
            status = "partial"
        assets.append(asset)
    return status, assets, findings, None


def normalized_rule(group, rule_id, protocol, action, source, port_range, region, instances):
    if not source or action not in {"allow", "accept"} or protocol not in {"tcp", "all"}:
        return None
    try:
        ports = str(port_range)
        if ports in {"-1/-1", "ALL", "all", "*"} and protocol == "all":
            first, last = 0, 65535
        else:
            matched = ports.replace("/", "-").split("-")
            first, last = int(matched[0]), int(matched[-1])
        if not is_public_ssh("ingress", "allow", protocol, source, first, last):
            return None
    except (ValueError, TypeError):
        return None
    return dict(kind="ssh", status="exposed", id=f"{group}:{rule_id}:{source}", name=group,
                region=region, source_cidr=source, first_port=first, last_port=last, instances=instances,
                protocol=protocol, direction="ingress", action="allow")


def qiniu_rules(adapter):
    assets, findings = {}, {}
    for alias in adapter.list_targets():
        data = adapter.inspect(alias)["data"]
        instance = data["instance"]
        for group, rules in data["security_group_ingress"].items():
            asset = assets.setdefault(group, dict(id=group, kind="security_group", name=group,
                region=instance.get("region_id"), instances=[]))
            asset["instances"].append(instance["instance_id"])
            for rule in rules:
                for source in (rule.get("source_cidr_ip"), rule.get("ipv6_source_cidr_ip")):
                    finding = normalized_rule(group, rule.get("id"), str(rule.get("protocol", "")).lower(),
                        str(rule.get("policy", "")).lower(), source, rule.get("port_range"), asset["region"], asset["instances"])
                    if finding:
                        findings[finding["id"]] = finding
    # Fixed target audits cover known instances, not every group in the account.
    return "partial", list(assets.values()), list(findings.values()), None


def cloud_rules(adapter, name, region):
    groups = adapter.security_groups(region)["items"]
    assets, findings = [], []
    status = "complete" if len(groups) <= 8 else "partial"
    for group in groups:
        key = group["SecurityGroupId"]
        asset = dict(id=key, kind="security_group", name=key, region=region, instances=[])
        assets.append(asset)
    for asset in assets[:8]:
        key = asset["id"]
        try:
            if name == "tencent-management":
                resources = adapter.security_group(region, key)["resources"]
                inbound = resources["policies"]["resources"]["Ingress"]
                rules = [rule["metadata"] for rule in inbound]
                asset["instances"] = [owner["metadata"]["InstanceId"] for owner in resources["owners"]]
                fields = ("PolicyIndex", "Protocol", "Action", "Port", "CidrBlock", "Ipv6CidrBlock")
            else:
                rules = []
                for rule in adapter.security_group_rules(region, key)["items"]:
                    direction = str(rule.get("Direction", "")).lower()
                    if direction == "ingress":
                        rules.append(rule)
                    elif direction != "egress":
                        status = "partial"
                fields = ("SecurityGroupRuleId", "IpProtocol", "Policy", "PortRange", "SourceCidrIp", "Ipv6SourceCidrIp")
        except (CatalogError, OSError, KeyError, TypeError, ValueError):
            status = "partial"
            continue
        for rule in rules:
            rid, protocol, action, port, ipv4, ipv6 = (rule.get(field) for field in fields)
            if ((not rid and rid != 0) or not (ipv4 or ipv6)
                    or not all(isinstance(value, str) and value for value in (protocol, action, port))):
                status = "partial"
                continue
            for source in (ipv4, ipv6):
                finding = normalized_rule(key, rid, str(protocol).lower(), str(action).lower(),
                                          source, port, region, asset["instances"])
                if finding:
                    findings.append(finding)
    return status, assets, findings, None
