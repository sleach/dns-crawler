# Copyright © 2019-2023 CZ.NIC, z. s. p. o.
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This file is part of dns-crawler.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import json
import re
import dns.resolver

from copy import deepcopy
from datetime import datetime
from socket import gethostname

from rq import get_current_connection

from .config_loader import default_config_filename, load_config
from .dns_utils import (annotate_dns_algorithm, check_dnssec,
                        get_local_resolver, get_ns_info, get_record,
                        get_record_parser, get_txt, parse_dmarc, parse_spf,
                        parse_tlsa, get_dkim)
from .geoip_utils import annotate_geoip, init_geoip
from .hsts_utils import get_hsts_status
from .ip_utils import get_source_addresses
from .mail_utils import get_mx_info
from .web_utils import get_webserver_info


def get_dns_local(domain, config, local_resolver, geoip_dbs):
    result = {}
    txt = get_record(domain, "TXT", local_resolver)
    result["NS_AUTH"] = get_record(domain, "NS", local_resolver)
    result["MAIL"] = get_record(domain, "MX", local_resolver)
    result["WEB4"] = annotate_geoip(get_record(domain, "A", local_resolver), geoip_dbs)
    if config["dns"]["check_www"]:
        result["WEB4_www"] = annotate_geoip(get_record("www." + domain, "A", local_resolver), geoip_dbs)
    result["WEB6"] = annotate_geoip(get_record(domain, "AAAA", local_resolver), geoip_dbs)
    if config["dns"]["check_www"]:
        result["WEB6_www"] = annotate_geoip(get_record("www." + domain, "AAAA", local_resolver), geoip_dbs)
    result["WEB_TLSA"] = parse_tlsa(get_record("_443._tcp." + domain, "TLSA", local_resolver))
    if config["dns"]["check_www"]:
        result["WEB_TLSA_www"] = parse_tlsa(get_record("_443._tcp.www." + domain, "TLSA", local_resolver))
    result["TXT"] = txt
    if txt:
        result["TXT_SPF"] = parse_spf(get_txt(re.compile('^"?v=spf'), deepcopy(txt)), domain)
    result["TXT_DMARC"] = parse_dmarc(get_record("_dmarc." + domain, "TXT", local_resolver), domain)
    result["TXT_openid"] = get_record("_openid." + domain, "TXT", local_resolver)
    result["TXT_MTA_STS"] = get_record("_mta-sts." + domain, "TXT", local_resolver)
    result["TXT_DKIM"] = get_dkim(domain, config["dns"]["dkim_selectors"], local_resolver)
    result["DS"] = annotate_dns_algorithm(get_record(domain, "DS", local_resolver), 1)
    result["DNSKEY"] = annotate_dns_algorithm(get_record(domain, "DNSKEY", local_resolver), 2)
    result["DNSSEC"] = check_dnssec(domain, local_resolver)
    additional = {}
    for record in config["dns"]["additional"]:
        values = get_record(domain, record, local_resolver)
        parser = get_record_parser(record)
        if parser is not None:
            additional[record] = parser(values, domain)
        else:
            additional[record] = values
    return dict(result, **additional)


def get_dns_auth(domain, nameservers, redis, config, local_resolver, geoip_dbs):
    source_ipv4, source_ipv6 = get_source_addresses(redis=redis, config=config)
    timeout = config["timeouts"]["dns"]
    cache_timeout = config["timeouts"]["cache"]
    chaosrecords = config["dns"]["auth_chaos_txt"]
    fingerprint_enabled = config["dns"]["fingerprint"]
    if not nameservers or len(nameservers) < 1:
        return None
    results = []
    for item in nameservers:
        ns = item["value"]
        if not ns:
            continue
        a = get_record(ns, "A", local_resolver)
        aaaa = get_record(ns, "AAAA", local_resolver)
        ipv4_results = []
        ipv6_results = []
        ns_resolver = dns.resolver.Resolver(configure=False)
        if a is not None and source_ipv4 is not None:
            for ipv4 in a:
                if "value" not in ipv4 or ipv4["value"] is None:
                    continue
                ns_resolver.nameservers = [ipv4["value"]]
                ns_info = get_ns_info(ipv4, domain, chaosrecords, geoip_dbs, timeout,
                                      cache_timeout, fingerprint_enabled, redis)
                try:
                    ns_info["glue"] = get_record(domain, "NS", ns_resolver)
                except (EOFError,
                        OSError,
                        TimeoutError,
                        dns.exception.Timeout,
                        dns.query.BadResponse,
                        dns.exception.FormError,
                        ConnectionRefusedError) as e:
                    ns_info["glue"] = {"error": str(e)}
                if ns_info:
                    ipv4_results.append(ns_info)
        if aaaa is not None and source_ipv6 is not None:
            for ipv6 in aaaa:
                if "value" not in ipv6 or ipv6["value"] is None:
                    continue
                ns_resolver.nameservers = [ipv6["value"]]
                ns_info = get_ns_info(ipv6, domain, chaosrecords, geoip_dbs, timeout,
                                      cache_timeout, fingerprint_enabled, redis)
                try:
                    ns_info["glue"] = get_record(domain, "NS", ns_resolver)
                except (EOFError,
                        OSError,
                        TimeoutError,
                        dns.exception.Timeout,
                        dns.query.BadResponse,
                        dns.exception.FormError,
                        ConnectionRefusedError) as e:
                    ns_info["glue"] = {"error": str(e)}
                if ns_info:
                    ipv6_results.append(ns_info)
        result = {
            "ns": ns,
        }
        if len(ipv4_results) > 0:
            result["ipv4"] = ipv4_results
        if len(ipv6_results) > 0:
            result["ipv6"] = ipv6_results
        results.append(result)
    return results


def get_web_status(domain, dns, config, source_ipv4, source_ipv6, geoip_dbs, path="/"):
    result = {}
    if config["web"]["check_ipv4"] and source_ipv4:
        if config["web"]["check_http"]:
            result["WEB4_80"] = get_webserver_info(domain, dns["WEB4"], config, source_ipv4, geoip_dbs, path=path)
        if config["dns"]["check_www"] and config["web"]["check_http"]:
            result["WEB4_80_www"] = get_webserver_info(f"www.{domain}", dns["WEB4_www"], config, source_ipv4,
                                                       geoip_dbs, path=path)
        if config["web"]["check_https"]:
            result["WEB4_443"] = get_webserver_info(domain, dns["WEB4"], config, source_ipv4, geoip_dbs, tls=True,
                                                    path=path)
        if config["dns"]["check_www"] and config["web"]["check_https"]:
            result["WEB4_443_www"] = get_webserver_info(f"www.{domain}", dns["WEB4_www"], config, source_ipv4,
                                                        geoip_dbs, tls=True, path=path)
    if config["web"]["check_ipv6"] and source_ipv6:
        if config["web"]["check_http"]:
            result["WEB6_80"] = get_webserver_info(domain, dns["WEB6"], config, source_ipv6, geoip_dbs, ipv6=True,
                                                   path=path)
        if config["dns"]["check_www"] and config["web"]["check_http"]:
            result["WEB6_80_www"] = get_webserver_info(f"www.{domain}", dns["WEB6_www"], config, source_ipv6, geoip_dbs,
                                                       ipv6=True, path=path)
        if config["web"]["check_https"]:
            result["WEB6_443"] = get_webserver_info(domain, dns["WEB6"], config, source_ipv6, geoip_dbs, ipv6=True,
                                                    tls=True, path=path)
        if config["dns"]["check_www"] and config["web"]["check_https"]:
            result["WEB6_443_www"] = get_webserver_info(f"www.{domain}", dns["WEB6_www"],
                                                        config, source_ipv6, geoip_dbs, ipv6=True, tls=True, path=path)
    return result


def process_domain(domain):
    redis = get_current_connection()
    hostname = gethostname()
    config = load_config(default_config_filename, redis=redis, hostname=hostname)
    source_ipv4, source_ipv6 = get_source_addresses(redis=redis, config=config)
    geoip_dbs = init_geoip(config)
    local_resolver = get_local_resolver(config)
    parent_zone = str(dns.name.from_text(domain).parent())
    parent_ns = get_record(parent_zone, "NS", local_resolver)
    parent_ns_info = get_dns_auth(domain, parent_ns, redis, config, local_resolver, geoip_dbs)
    dns_local = get_dns_local(domain, config, local_resolver, geoip_dbs)
    dns_auth = get_dns_auth(domain, dns_local["NS_AUTH"], redis, config, local_resolver, geoip_dbs)
    if dns_local["MAIL"]:
        mail = get_mx_info(dns_local["MAIL"], config["mail"]["ports"], geoip_dbs, config["timeouts"]["mail"],
                           config["mail"]["get_banners"], config["timeouts"]["cache"],
                           local_resolver, redis, source_ipv4, source_ipv6, config["mail"]["max_ips_per_host"])
    elif dns_local["WEB4"] or dns_local["WEB6"]:
        mail = get_mx_info([{"value": domain}], config["mail"]["ports"], geoip_dbs, config["timeouts"]["mail"],
                           config["mail"]["get_banners"], config["timeouts"]["cache"],
                           local_resolver, redis, source_ipv4, source_ipv6, config["mail"]["max_ips_per_host"])
    else:
        mail = None
    web = get_web_status(domain, dns_local, config, source_ipv4, source_ipv6, geoip_dbs)
    hsts = get_hsts_status(domain)

    fetch_web_paths = "paths" in config["web"] and len(config["web"]["paths"]) > 0

    if fetch_web_paths:
        web_paths = {}
        for path in config["web"]["paths"]:
            web_paths[path] = get_web_status(domain, dns_local, config, source_ipv4, source_ipv6, path=path)

    result = {
        "domain": domain,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "parent": {
            "zone": parent_zone,
            "ns": parent_ns_info
        },
        "results": {
            "DNS_LOCAL": dns_local,
            "DNS_AUTH": dns_auth,
            "MAIL": mail,
            "WEB": web,
            "HSTS": hsts
        }
    }

    if fetch_web_paths:
        result["results"]["WEB_paths"] = web_paths

    if config["save_worker_hostname"]:
        result["worker_hostname"] = hostname

    return result


def get_json_result(domain):
    return json.dumps(process_domain(domain), ensure_ascii=False, check_circular=False, separators=(",", ":"))
