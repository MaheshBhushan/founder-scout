"""Explicit application fallback; never used by automated Founder Scout discovery."""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit

import httpx

from .outreach import ROLE_INBOXES, tracker_setting
from .scout import GENERIC_LOCAL

MAX_CANDIDATES = 8
REACHER_TIMEOUT = 30.0
GENERIC_INBOXES = GENERIC_LOCAL | ROLE_INBOXES | {
    "admin", "administrator", "webmaster", "hello", "recruitment", "recruiting", "noreply", "donotreply",
}
REACHER_FIELDS = (
    ("syntax", "is_valid_syntax", True),
    ("mx", "accepts_mail", True),
    ("smtp", "can_connect_smtp", True),
    ("smtp", "is_deliverable", True),
    ("smtp", "is_catch_all", False),
    ("smtp", "is_disabled", False),
    ("smtp", "has_full_inbox", False),
    ("misc", "is_disposable", False),
    ("misc", "is_role_account", False),
)


class ManifestError(ValueError):
    """A manifest cannot provide a valid researched recipient or draft."""


def required_text(data: dict, key: str, limit: int | None = None) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{key} must be a nonempty string")
    if limit is not None and len(value) > limit:
        raise ManifestError(f"{key} exceeds {limit} characters; do not truncate evidence")
    return value


def normalize_domain(value: str) -> str:
    if value != value.strip() or any(c in value for c in "/:@?#\\"):
        raise ManifestError("domain must be a bare company domain, not a URL")
    try:
        host = value.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ManifestError("domain is invalid") from exc
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels
    ) or not re.fullmatch(r"[a-z]{2,}|xn--[a-z0-9-]+", labels[-1]):
        raise ManifestError("domain must be a valid fully qualified company domain")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ManifestError("domain must not be an IP address")


def http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (parsed.scheme in ("http", "https") and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
                and not any(c.isspace() for c in value) and parsed.port != 0)
    except ValueError:
        return False


def name_token(value: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]", "", ascii_name)


def validate_manifest(manifest: object, queue: bool = False) -> dict:
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be a JSON object")
    # Accept a named contact section as well as a flat contact manifest.
    contact = manifest.get("contact", manifest)
    if not isinstance(contact, dict):
        raise ManifestError("contact must be an object")
    name = required_text(contact, "name", 200)
    if name != name.strip():
        raise ManifestError("name must not have leading or trailing whitespace")
    host = normalize_domain(required_text(contact, "domain"))
    source_url = required_text(contact, "source_url", 2000)
    excerpt = required_text(contact, "source_excerpt", 1000)
    if not http_url(source_url):
        raise ManifestError("source_url must be an http(s) URL")
    if name not in excerpt:
        raise ManifestError("source_excerpt must literally contain the researched person's name")
    result = {"name": name, "domain": host, "source_url": source_url, "source_excerpt": excerpt}
    for key in ("company", "role"):
        result[key] = required_text(contact, key, 200)
    location = contact.get("location", "")
    if not isinstance(location, str):
        raise ManifestError("location must be a string")
    result["location"] = location
    emails = manifest.get("emails", contact.get("emails", []))
    source_host = normalize_domain(urlsplit(source_url).hostname or "")
    if source_host != host and not source_host.endswith(f".{host}"):
        raise ManifestError("source_url must be on the official company domain or a subdomain")
    if not isinstance(emails, list) or len(emails) > MAX_CANDIDATES:
        raise ManifestError(f"emails must be a list of at most {MAX_CANDIDATES} researched addresses")
    normalized = []
    for email in emails:
        if not isinstance(email, str) or email.count("@") != 1:
            raise ManifestError("emails must contain complete email addresses")
        local, email_host = email.lower().split("@")
        if (len(local) > 64 or not re.fullmatch(r"[a-z0-9!#$%&'*+/=?^_`{|}~.-]+", local)
                or local.startswith(".") or local.endswith(".") or ".." in local
                or normalize_domain(email_host) != host):
            raise ManifestError("every researched email must be valid and use the contact's domain")
        address = f"{local}@{host}"
        if address not in normalized:
            normalized.append(address)
    result["emails"] = normalized
    outreach = manifest.get("outreach")
    if queue and outreach is None:
        raise ManifestError("--queue requires outreach with subject, body and purpose")
    if outreach is not None:
        if not isinstance(outreach, dict):
            raise ManifestError("outreach must be an object")
        result["outreach"] = {
            "subject": required_text(outreach, "subject", 300),
            "body": required_text(outreach, "body", 10000),
            "purpose": required_text(outreach, "purpose", 500),
            "campaign": outreach.get("campaign", "tailor-contact-fallback"),
        }
        required_text(result["outreach"], "campaign", 100)
    return result


def candidate_emails(contact: dict) -> list[str]:
    addresses = list(contact["emails"])
    parts = contact["name"].split()
    if len(parts) >= 2:
        first, last = name_token(parts[0]), name_token(parts[-1])
        if first and last:
            for local in (f"{first}.{last}", f"{first}{last}", f"{first[0]}.{last}",
                          f"{first[0]}{last}", first, f"{first}_{last}", f"{last}.{first}"):
                address = f"{local}@{contact['domain']}"
                if len(local) <= 64 and address not in addresses and len(addresses) < MAX_CANDIDATES:
                    addresses.append(address)
    if not addresses:
        raise ManifestError("inferred email formats require at least two usable name parts; otherwise provide researched emails")
    return addresses


def generic_email(email: str) -> bool:
    local = email.split("@", 1)[0].split("+", 1)[0]
    return re.sub(r"[._-]", "", local) in GENERIC_INBOXES


def verification_reasons(email: str, result: object) -> list[str]:
    if not isinstance(result, dict):
        return ["invalid_response"]
    reasons = []
    if result.get("input") != email:
        reasons.append("input_mismatch")
    if result.get("is_reachable") != "safe":
        reasons.append("is_reachable_not_safe")
    for values in (result, *(result.get(section) for section in ("syntax", "mx", "smtp", "misc"))):
        if isinstance(values, dict) and (
            values.get("error") is not None or values.get("errors") not in (None, [])
        ):
            reasons.append("upstream_error")
    for section, key, expected in REACHER_FIELDS:
        values = result.get(section)
        if not isinstance(values, dict) or values.get(key) is not expected:
            reasons.append(f"{section}.{key}_must_be_{str(expected).lower()}")
    return reasons


async def check_candidate(http: httpx.AsyncClient, email: str, base: str, key: str) -> dict:
    if generic_email(email):
        return {"email": email, "status": "rejected", "reasons": ["generic_inbox"], "result": None}
    if not base or not http_url(base):
        return {"email": email, "status": "unavailable", "reasons": ["reacher_not_configured"], "result": None}
    headers = {"user-agent": "founder-scout/1"}
    if key:
        headers["x-reacher-secret"] = key
    try:
        response = await http.post(f"{base.rstrip('/')}/v0/check_email", json={"to_email": email},
                                   headers=headers, timeout=REACHER_TIMEOUT)
        response.raise_for_status()
        result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"email": email, "status": "unavailable", "reasons": [type(exc).__name__], "result": None}
    reasons = verification_reasons(email, result)
    status = "rejected" if reasons else "safe"
    if (not isinstance(result, dict) or result.get("input") != email
            or result.get("is_reachable") not in ("safe", "risky", "invalid")
            or (result.get("is_reachable") == "safe" and reasons)):
        status = "inconclusive"
    return {"email": email, "status": status, "reasons": reasons, "result": result}


def tracker_payload(contact: dict, email: str) -> dict:
    return {
        "to_email": email, "to_name": contact["name"], "to_company": contact["company"] or None,
        "to_role": contact["role"] or None, "source_url": contact["source_url"],
        "source_excerpt": contact["source_excerpt"], **contact["outreach"], "send": True,
        "email_verification": {"method": "reacher", "name": contact["name"], "domain": contact["domain"]},
    }


async def resolve_contact(manifest: object, queue: bool = False, *, http: httpx.AsyncClient | None = None) -> dict:
    contact = validate_manifest(manifest, queue)
    emails = candidate_emails(contact)
    if http is None:
        async with httpx.AsyncClient(timeout=REACHER_TIMEOUT, follow_redirects=False) as client:
            return await resolve_contact(manifest, queue, http=client)
    base = tracker_setting("REACHER_URL", "reacher-url")
    key = tracker_setting("REACHER_API_KEY", "reacher-key")
    candidates = [await check_candidate(http, email, base, key) for email in emails]
    safe = [c["email"] for c in candidates if c["status"] == "safe"]
    selected = None
    if len(safe) > 1:
        decision, reason = "ambiguous", "multiple safe mailboxes; recipient ownership is unknown"
    elif any(c["status"] == "unavailable" for c in candidates):
        decision, reason = "verification_unavailable", "every candidate must be checked to establish a unique safe mailbox"
    elif any(c["status"] == "inconclusive" for c in candidates):
        decision, reason = "verification_failed", "unknown or malformed results cannot establish unique recipient ownership"
    elif not safe:
        decision, reason = "verification_failed", "no candidate passed all Reacher checks"
    else:
        selected = safe[0]
        decision, reason = "verified", "exactly one candidate passed all Reacher checks"
    output = {"decision": decision, "reason": reason, "selected_address": selected, "candidates": candidates}
    if selected and "outreach" in contact:
        payload = tracker_payload(contact, selected)
        output["payload"] = payload
        if queue:
            url = tracker_setting("TAILOR_TRACKER_URL", "url")
            token = tracker_setting("TAILOR_TRACKER_TOKEN", "token")
            if not url or not http_url(url) or not token:
                output.update(decision="queue_unavailable", reason="configure TAILOR_TRACKER_URL and TAILOR_TRACKER_TOKEN")
                return output
            try:
                response = await http.post(f"{url}/api/outreach", json=payload, timeout=90.0,
                                           headers={"authorization": f"Bearer {token}", "user-agent": "founder-scout/1"})
                response.raise_for_status()
                tracker = response.json()
                if not isinstance(tracker, dict) or not isinstance(tracker.get("outreach"), str):
                    raise ValueError("invalid tracker response")
            except (httpx.HTTPError, ValueError) as exc:
                output.update(decision="queue_error", reason=f"tracker request failed: {exc}")
            else:
                # The tracker independently rechecks verification, evidence, dedup and shared caps.
                output.update(decision=tracker["outreach"], reason=f"tracker decision: {tracker['outreach']}", tracker=tracker)
    return output
