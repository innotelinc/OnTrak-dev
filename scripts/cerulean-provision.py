#!/usr/bin/env python3
"""Provision OnTrak's public names through Cerulean — DNS, TLS and the edge.

OnTrak does not own a CA, a nameserver or an edge. Cerulean does (see
docs/stack.md), and it is the only thing that may write these records: a name
added by hand in Technitium and a certificate fetched by hand from ACME are two
things that drift apart, and the drift shows up as a browser warning in front of
a class.

Three names, one range, one certificate family:

    ontrak.innotel.us           the range itself — the portal, and the
                                `/guacamole/` path the console is served from
    student.ontrak.innotel.us   what a student opens
    admin.ontrak.innotel.us     what an instructor opens

A wildcard (`*.ontrak.innotel.us`) covers the two subdomains; the apex needs its
own certificate, because a wildcard never covers the name it hangs off. Both are
issued through Cerulean over DNS-01, so nothing has to be reachable from the
internet for the certificate to exist.

Auth is a **service key** (`ceru_…`), which is the only unattended way in:
Cerulean's local password is break-glass and is refused unless the host sets
BREAKGLASS_LOGIN=1, and a session exists only for a browser OIDC flow. Mint one
as platform admin — Platform → *Service API keys* — with the scopes this needs:

    domains, dns, certs, npm

    Cerulean · Platform · Service API keys → name "ontrak", scopes as above

Usage:

    scripts/cerulean-provision.py                 # plan only, changes nothing
    scripts/cerulean-provision.py --apply

Configuration (environment; every one has a default that matches the estate):

    CERULEAN_API_URL        https://cerulean.innotel.us
    CERULEAN_API_TOKEN      the ceru_ service key (required to do anything)
    ONTRAK_DNS_ZONE         innotel.us      — the zone the names live in
    ONTRAK_EDGE_IP          73.68.203.71    — what the names resolve to (the edge)
    ONTRAK_FORWARD_HOST     192.168.1.46    — where the edge forwards: the range's
                                              gateway. Set this when the range
                                              runs elsewhere (pass --repoint to
                                              move a live name).
    ONTRAK_PORTAL_PORT      8080    — the one port the edge forwards

Exit codes: 0 = the plan ran (or was applied) cleanly, 1 = a step failed,
2 = cannot run (no key, unreachable).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_NAME = "ontrak.innotel.us"
STUDENT_NAME = "student.ontrak.innotel.us"
ADMIN_NAME = "admin.ontrak.innotel.us"
WILDCARD = "*.ontrak.innotel.us"

# The console is a path on the range's own name, which is what
# ONTRAK_GUAC__BASE_URL points at. Cerulean's NPM bridge forwards a host, not a
# path (see docs/docker.md), so the location rule is the one thing here that is
# applied by talking to NPM itself — and it is therefore opt-in and reported.
CONSOLE_PATH = "/guacamole"


def setting(name: str, default: str = "") -> str:
    value = (os.environ.get(name) or "").strip()
    return value or default


class CannotRun(Exception):
    """Configuration or reachability problem — exit 2, not a failed step."""


class StepFailed(Exception):
    """A call to Cerulean said no — exit 1."""


# ── Cerulean, over the service bridge ───────────────────────────────────────
# Cerulean answers on one path space for a signed-in session (`/api/domains`) and
# another for a service key (`/api/service/domains`). Only the second is
# reachable without a browser, so this file speaks only that one — a caller that
# silently fell back to the session space would work on a laptop and fail in a
# scheduled job.
SERVICE_PATHS = {
    "/api/domains": "/api/service/domains",
    "/api/dns/records": "/api/service/dns/records",
    "/api/certificates": "/api/service/certificates",
    "/api/npm/export-cert": "/api/service/npm/export-cert",
    "/api/npm/hosts": "/api/service/npm/hosts",
}

# The bridge's record route is zone-addressed rather than id-addressed
# (`/api/domains/<id>/records` becomes `/api/service/dns/records?zone=<zone>`),
# because a key carries a tenant and a tenant has many zones — the id would have
# to be resolved to a name anyway. Forgetting the query is not a quiet failure:
# Cerulean answers `?zone= is required`.
RECORDS_PATH = "/api/dns/records"


class Api:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.zone = ""
        # Whether that zone exists yet. A plan has to be able to run against an
        # estate that does not have the zone registered — that is the first run —
        # and its records cannot be read before it does.
        self.registered = False

    def _path(self, path: str) -> str:
        if path == RECORDS_PATH:
            if not self.zone:
                raise CannotRun("the zone must be known before records can be read")
            return f"{SERVICE_PATHS[RECORDS_PATH]}?zone={urllib.parse.quote(self.zone)}"
        for session, service in SERVICE_PATHS.items():
            if path == session:
                return service
            if path.startswith(session + "/"):
                return service + path[len(session):]
        raise CannotRun(f"{path} has no service-bridge equivalent")

    def call(self, path: str, method: str = "GET", body: dict | None = None, timeout: int = 60):
        """One request; `(status, payload)` so the caller decides. Failures come
        back as Cerulean's own words rather than a traceback, because "Insufficient
        scope — requires: dns" is the answer that saves an afternoon."""
        url = f"{self.base}{self._path(path)}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace").strip()
            with contextlib.suppress(json.JSONDecodeError):
                detail = json.loads(detail).get("error") or detail
            return error.code, detail[:300]
        except (urllib.error.URLError, OSError) as error:
            raise CannotRun(
                f"{url}: {type(error).__name__}: {getattr(error, 'reason', error)}"
            ) from error

    def get_list(self, path: str, what: str) -> list[dict]:
        status, payload = self.call(path)
        if status != 200:
            raise StepFailed(f"could not read {what} (HTTP {status}): {payload}")
        items = payload.get(what.split()[0], payload) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise StepFailed(f"unexpected {what} from Cerulean: {str(payload)[:200]}")
        return items


# ── the small pure parts, which is where the mistakes live ──────────────────


def zone_relative(fqdn: str, zone: str) -> str:
    """`student.ontrak.innotel.us` in zone `innotel.us` -> `student.ontrak`.

    Technitium's create call takes a zone-relative name while its reads come back
    fully qualified, so this conversion happens once, here, rather than at each
    call site guessing which form arrived.
    """
    suffix = "." + zone.rstrip(".").lower()
    name = fqdn.rstrip(".").lower()
    return name[: -len(suffix)] if name.endswith(suffix) else name


def record_value(record: dict) -> str:
    """Technitium spells the payload `rData`; some responses say `value`."""
    return str(record.get("rData") or record.get("value") or "").rstrip(".").lower()


def find_record(records: list[dict], fqdn: str, record_type: str) -> dict | None:
    wanted = fqdn.rstrip(".").lower()
    for record in records:
        if str(record.get("name", "")).rstrip(".").lower() != wanted:
            continue
        if str(record.get("type", "")).upper() == record_type.upper():
            return record
    return None


def certificate_body(fqdn: str) -> dict:
    """The request body for a name.

    A wildcard is spelled differently from what it looks like: `domain` must be
    the *base* — Cerulean validates it as `/^[a-z0-9.-]+$/`, so a literal `*`
    there is refused with `Invalid domain name` — and `wildcard: true` is what
    makes the issued certificate carry `*.base`.
    """
    if fqdn.startswith("*."):
        return {"domain": fqdn[2:], "wildcard": True, "name": fqdn}
    return {"domain": fqdn, "name": fqdn}


def certificate_names(cert: dict) -> list[str]:
    """Every name a certificate row carries, including the wildcard it *is*.

    Cerulean records a wildcard as `domain: <base>` plus `wildcard: true`, and its
    `domains` list is not guaranteed to spell the `*.` out — so the flag is read
    rather than the list trusted. Read from Cerulean, not from NPM: NPM rewrites an
    uploaded certificate's `domain_names` to its CN, so a wildcard is listed there
    as its bare base and a coverage check against NPM finds nothing.
    """
    names = [str(n) for n in (cert.get("domains") or [])]
    domain = cert.get("domain")
    if isinstance(domain, str) and domain:
        names.append(domain)
        if cert.get("wildcard") is True:
            names.append("*." + domain.lstrip("*."))
    return names


def covers(names: list[str], fqdn: str) -> bool:
    """Whether one of `names` covers `fqdn`, wildcards included (RFC 6125).

    `*.ontrak.innotel.us` covers `student.ontrak.innotel.us` and nothing deeper,
    and it does **not** cover `ontrak.innotel.us` — that only happens because
    Cerulean's row for a wildcard also lists the base, which is why the base is in
    `certificate_names` at all. Being permissive here attaches a certificate to a
    host it does not cover, which the edge serves as a browser warning rather than
    an error anyone reads.
    """
    wanted = fqdn.rstrip(".").lower()
    for raw in names:
        name = str(raw).rstrip(".").lower()
        if not name:
            continue
        if name == wanted:
            return True
        if not name.startswith("*.") or wanted.count(".") != name.count("."):
            continue
        if wanted.endswith(name[1:]):
            return True
    return False


# Cerulean reports `expiresAt` in OpenSSL's `notAfter` spelling
# (`Dec 17 19:14:45 2026 GMT`), not ISO 8601. Treating that as unparseable made
# every plan say "would request a certificate" for names that already had a
# healthy one, and every --apply mint two fresh Let's Encrypt certificates.
_OPENSSL_EXPIRY = "%b %d %H:%M:%S %Y"


def parse_expiry(value: str) -> dt.datetime | None:
    text = str(value).strip()
    # OpenSSL always prints the zone as GMT; the value is UTC either way.
    openssl = text[: -len(" GMT")] if text.endswith(" GMT") else text
    for parse in (
        lambda: dt.datetime.fromisoformat(text.replace("Z", "+00:00")),
        lambda: dt.datetime.strptime(openssl, _OPENSSL_EXPIRY),
    ):
        try:
            when = parse()
        except ValueError:
            continue
        return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)
    return None


def select_certificate(
    certificates: list[dict], fqdn: str, renew_days: int, now: dt.datetime | None = None
) -> dict | None:
    """The existing certificate that can be reused for `fqdn`, if there is one.

    Two things are deliberate. Material has to be present: an `issued` row with no
    PEM is what a failed export leaves behind, and pointing a host at it produces a
    TLS error at the edge that nothing here could act on. And a certificate inside
    the renewal window is not reused — issuing a replacement is the cheap half of
    that trade. An exact name beats a wildcard, so the range's own name does not
    borrow the certificate that exists for its two subdomains.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    horizon = now + dt.timedelta(days=renew_days)
    wanted = fqdn.rstrip(".").lower()
    best: tuple[int, dt.datetime] | None = None
    best_cert: dict | None = None
    for cert in certificates:
        names = certificate_names(cert)
        if not covers(names, fqdn):
            continue
        if cert.get("status") != "issued" or not cert.get("hasMaterial"):
            continue
        expires = parse_expiry(cert.get("expiresAt") or "")
        if not expires or expires < horizon:
            continue
        exact = 1 if wanted in [str(n).rstrip(".").lower() for n in names] else 0
        rank = (exact, expires)
        if best is None or rank > best:
            best, best_cert = rank, cert
    return best_cert


def find_proxy_host(hosts: list[dict], fqdn: str) -> dict | None:
    """The NPM host serving exactly this name.

    Matched on the whole name, not a prefix: `admin.ontrak.innotel.us` and
    `admin-old.ontrak.innotel.us` share a prefix, and repointing the wrong one is
    a live range going dark.
    """
    wanted = fqdn.rstrip(".").lower()
    for host in hosts:
        names = [str(n).rstrip(".").lower() for n in (host.get("domain_names") or [])]
        if wanted in names:
            return host
    return None


# ── the steps ───────────────────────────────────────────────────────────────


def ensure_zone(api: Api, zone: str, apply: bool) -> str:
    """The zone the names live in, registered with Cerulean.

    Registering is what makes Cerulean create and track the zone in Technitium;
    skipping it is how a record ends up in a zone nobody is watching.
    """
    api.zone = zone
    zones = api.get_list("/api/domains", "domains")
    for existing in zones:
        if str(existing.get("name", "")).rstrip(".").lower() == zone.lower():
            api.registered = True
            return f"zone {zone} is registered (id {existing.get('id')})"
    if not apply:
        return f"would register zone {zone} (its records cannot be read yet)"
    status, created = api.call("/api/domains", "POST", {"name": zone})
    if status not in (200, 201):
        raise StepFailed(f"could not register zone {zone} (HTTP {status}): {created}")
    api.registered = True
    return f"registered zone {zone}"


def ensure_record(api: Api, fqdn: str, record_type: str, value: str, apply: bool) -> str:
    """Create the record if it is absent; never silently change one that exists."""
    if not api.zone:
        raise CannotRun("the zone must be known before records can be read")
    if not api.registered:
        # Unregistered zone, and only reachable in a plan: the records of a zone
        # that does not exist yet cannot be read, so the plan says what it would
        # create and whether it would create it. --apply registers the zone first
        # and always comes back through here with it registered.
        if apply:
            raise StepFailed(f"zone {api.zone} was not registered, so {fqdn} cannot be created")
        return f"would create {fqdn} {record_type} -> {value} (zone {api.zone} is not registered yet)"
    status, payload = api.call("/api/dns/records", "GET", None)
    if status != 200:
        raise StepFailed(f"could not read zone records (HTTP {status}): {payload}")
    records = payload.get("records", payload) if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise StepFailed(f"unexpected record list: {str(payload)[:200]}")

    existing = find_record(records, fqdn, record_type)
    if existing is not None:
        found = record_value(existing)
        if found != value.lower():
            raise StepFailed(
                f"{fqdn} already has {record_type} -> {found}, not {value}. "
                "Refusing to change it; remove the record first if it is wrong."
            )
        return f"{fqdn} {record_type} already -> {found}"
    if not apply:
        return f"would create {fqdn} {record_type} -> {value}"
    status, created = api.call(
        "/api/dns/records",
        "POST",
        {
            "zone": api.zone,
            "type": record_type,
            "name": zone_relative(fqdn, api.zone),
            "value": value,
            "ttl": 300,
        },
    )
    if status not in (200, 201):
        raise StepFailed(f"could not create {fqdn} (HTTP {status}): {created}")
    return f"created {fqdn} {record_type} -> {value}"


def ensure_certificate(
    api: Api, fqdn: str, apply: bool, wait: int, renew_days: int
) -> tuple[int | None, str]:
    """A certificate covering `fqdn`, issued if there is not one already."""
    certificates = api.get_list("/api/certificates", "certificates")
    reusable = select_certificate(certificates, fqdn, renew_days)
    if reusable is not None:
        # select_certificate only returns rows whose expiry parsed.
        expires = parse_expiry(reusable["expiresAt"]).date().isoformat()
        return reusable["id"], f"certificate #{reusable['id']} covers {fqdn} (expires {expires})"
    if not apply:
        return None, f"would request a certificate for {fqdn}"

    status, created = api.call("/api/certificates", "POST", certificate_body(fqdn))
    if status not in (200, 201, 202) or not isinstance(created, dict) or not created.get("id"):
        raise StepFailed(f"could not request a certificate for {fqdn} (HTTP {status}): {created}")
    cert_id = int(created["id"])

    state, deadline = "pending", time.monotonic() + wait
    while time.monotonic() < deadline:
        status, cert = api.call(f"/api/certificates/{cert_id}")
        if status != 200 or not isinstance(cert, dict):
            raise StepFailed(f"could not poll certificate #{cert_id} (HTTP {status}): {cert}")
        state = cert.get("status")
        if state == "issued" and cert.get("hasMaterial"):
            return cert_id, f"issued certificate #{cert_id} for {fqdn}"
        if state in ("failed", "error"):
            raise StepFailed(f"certificate #{cert_id} {state}: {cert.get('error')}")
        time.sleep(6)
    raise StepFailed(
        f"certificate #{cert_id} is still {state} after {wait}s — it may still finish, "
        f"so check /api/service/certificates/{cert_id} before re-running"
    )


def export_to_npm(api: Api, certificate_id: int) -> int:
    """Push the PEM material into NPM, returning NPM's own certificate id."""
    status, exported = api.call("/api/npm/export-cert", "POST", {"certificate_id": certificate_id})
    if status not in (200, 201) or not isinstance(exported, dict) or not exported.get("npmCertificateId"):
        raise StepFailed(f"could not export certificate #{certificate_id} to NPM (HTTP {status}): {exported}")
    return int(exported["npmCertificateId"])


def ensure_proxy_host(
    api: Api,
    fqdn: str,
    forward_host: str,
    forward_port: int,
    certificate_id: int | None,
    apply: bool,
    repoint: bool = False,
) -> str:
    """Create (or verify) the edge host for `fqdn`, with TLS enforced."""
    hosts = api.get_list("/api/npm/hosts", "hosts")
    existing = find_proxy_host(hosts, fqdn)
    wanted = f"http://{forward_host}:{forward_port}"

    if existing is not None:
        target = (
            f"{existing.get('forward_scheme')}://{existing.get('forward_host')}:{existing.get('forward_port')}"
        )
        if target == wanted:
            return f"{fqdn} already -> {target} (host #{existing.get('id')})"
        if not repoint:
            raise StepFailed(
                f"NPM already serves {fqdn} -> {target}, not {wanted}. Refusing to repoint a "
                "name that is live; pass --repoint if the backend really moved."
            )
        if not apply:
            return f"would repoint {fqdn} {target} -> {wanted}"
        status, updated = api.call(
            f"/api/npm/hosts/{existing.get('id')}",
            "PUT",
            {
                "domain": fqdn,
                "forward_host": forward_host,
                "forward_port": forward_port,
                "forward_scheme": "http",
                "certificate_id": export_to_npm(api, certificate_id) if certificate_id else None,
                "ssl_forced": True,
                "http2_support": True,
            },
        )
        if status not in (200, 201):
            raise StepFailed(f"could not repoint {fqdn} (HTTP {status}): {updated}")
        return f"repointed {fqdn} -> {wanted}"

    if not apply:
        return f"would create an NPM proxy host {fqdn} -> {wanted}"
    if certificate_id is None:
        raise StepFailed(f"no certificate to attach for {fqdn} — refusing to serve it over plain HTTP")
    npm_cert = export_to_npm(api, certificate_id)
    status, host = api.call(
        "/api/npm/hosts",
        "POST",
        {
            "domain": fqdn,
            "forward_host": forward_host,
            "forward_port": forward_port,
            "forward_scheme": "http",
            "certificate_id": npm_cert,
            "ssl_forced": True,
            "http2_support": True,
        },
    )
    if status not in (200, 201) or not isinstance(host, dict):
        raise StepFailed(f"could not create the proxy host for {fqdn} (HTTP {status}): {host}")
    return f"created NPM proxy host #{host.get('id')} {fqdn} -> {wanted} (TLS enforced)"


def plan(args) -> int:
    token = setting("CERULEAN_API_TOKEN")
    base = setting("CERULEAN_API_URL", "https://cerulean.innotel.us")
    zone = setting("ONTRAK_DNS_ZONE", "innotel.us")
    edge_ip = setting("ONTRAK_EDGE_IP", "73.68.203.71")
    forward_host = setting("ONTRAK_FORWARD_HOST", "192.168.1.46")
    portal_port = int(setting("ONTRAK_PORTAL_PORT", "8080"))

    if not token:
        raise CannotRun(
            "CERULEAN_API_TOKEN is not set. Cerulean's only unattended door is a service "
            "key (ceru_…), minted as platform admin under Platform → Service API keys, with "
            "the scopes domains, dns, certs, npm."
        )
    if not token.startswith("ceru_"):
        raise CannotRun("CERULEAN_API_TOKEN does not look like a service key (they start with ceru_)")

    api = Api(base, token)
    print(f"cerulean  {base}")
    print(f"zone      {zone}   names -> {edge_ip}   edge -> {forward_host}")
    print(f"mode      {'apply' if args.apply else 'dry run (nothing is written)'}\n")

    # Printed as they happen, not collected: when a step fails in the middle of a
    # plan, the steps that already succeeded are half the diagnosis.
    def emit(step: str) -> None:
        print(f"  · {step}", flush=True)

    emit(ensure_zone(api, zone, args.apply))

    for fqdn in (BASE_NAME, STUDENT_NAME, ADMIN_NAME):
        emit(ensure_record(api, fqdn, "A", edge_ip, args.apply))

    # Two certificates: the wildcard covers both subdomains, the apex needs its
    # own — a wildcard never covers the name it hangs off.
    wildcard_id, wildcard_report = ensure_certificate(api, WILDCARD, args.apply, args.wait, args.renew_days)
    emit(wildcard_report)
    if args.apply and wildcard_id:
        emit(f"exported the wildcard to NPM (certificate #{export_to_npm(api, wildcard_id)})")
    apex_id, apex_report = ensure_certificate(api, BASE_NAME, args.apply, args.wait, args.renew_days)
    emit(apex_report)
    if args.apply and apex_id:
        emit(f"exported the apex certificate to NPM (certificate #{export_to_npm(api, apex_id)})")

    for fqdn, cert_id, port in (
        (BASE_NAME, apex_id, portal_port),
        (STUDENT_NAME, wildcard_id, portal_port),
        (ADMIN_NAME, wildcard_id, portal_port),
    ):
        emit(ensure_proxy_host(api, fqdn, forward_host, port, cert_id, args.apply, args.repoint))

    if args.apply:
        print(
            f"\nthe range is on the edge. Every name forwards to one address, and that is\n"
            "enough: the stack's own gateway serves the portal at / and the console at\n"
            f"{CONSOLE_PATH}/ on that same port, so the edge needs no location rule —\n"
            "Cerulean's NPM bridge forwards a host rather than a path, and this is the\n"
            "arrangement that does not ask it to.\n"
            f"    {BASE_NAME}/  and  {BASE_NAME}{CONSOLE_PATH}/  ->  http://{forward_host}:{portal_port}\n"
            f"Set ONTRAK_GUAC__BASE_URL=https://{BASE_NAME}{CONSOLE_PATH}/ on the range."
        )
    else:
        print("\nnothing was written — re-run with --apply")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the changes (default: plan only)")
    parser.add_argument(
        "--repoint",
        action="store_true",
        help="allow an existing edge host to be pointed at a different backend",
    )
    parser.add_argument("--wait", type=int, default=180, help="seconds to wait for a certificate")
    parser.add_argument("--renew-days", type=int, default=21, help="reissue inside this window")
    args = parser.parse_args()
    try:
        return plan(args)
    except CannotRun as error:
        print(f"cannot run: {error}", file=sys.stderr)
        return 2
    except StepFailed as error:
        print(f"failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
