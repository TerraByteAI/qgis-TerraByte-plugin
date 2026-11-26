"""Which notice this build shows, which terms version it records, and when the new-terms features are on.

A build records only the version of the notice it shows.  The service reports one ``meta.terms_version`` for older
builds and lists every version it knows in ``meta.terms_versions``, oldest first.

* **New-terms mode**: ``NOTICE_TERMS_VERSION`` is in ``meta.terms_versions``.  The account's latest accepted version
  (``me.consent.latest_version``) decides the notice: none -> EN-BeforeStart ("first"), lower than this build's ->
  EN-TermsUpdated ("updated", once per account), equal or higher -> nothing.  "I agree" posts ``NOTICE_TERMS_VERSION``;
  every run create carries it (``run_create_fields``).
* **Legacy mode** (an older service: no ``terms_versions``, or it does not list this build's version): the account's
  ``accepted_terms_version`` is compared with ``meta.terms_version`` and "I agree" posts ``meta.terms_version``; run
  create carries no ``terms_version``.  The dialog still shows this build's wording.

Pure functions of ``ctx.meta`` and ``ctx.account``; nothing here talks to the network.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

NOTICE_TERMS_VERSION = "2026-11-24"

TERMS_URL = "https://terrabyte.ai/privacy"
PRIVACY_URL = "https://terrabyte.ai/privacy"

FIRST = "first"
UPDATED = "updated"


def _versions(meta: Optional[Dict[str, Any]]) -> List[str]:
    v = (meta or {}).get("terms_versions")
    if not isinstance(v, (list, tuple)):
        return []
    return [str(x) for x in v if isinstance(x, str) and x]


def new_terms_mode(meta: Optional[Dict[str, Any]]) -> bool:
    """The service lists this build's notice version: the notice is chosen per account and run creates record it."""
    return NOTICE_TERMS_VERSION in _versions(meta)


def _consent(account: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    c = (account or {}).get("consent")
    return c if isinstance(c, dict) else {}


def latest_accepted(account: Optional[Dict[str, Any]]) -> Optional[str]:
    """The account's latest accepted terms version: ``consent.latest_version`` from a service that has it, else
    ``consent.accepted_terms_version`` (an older service)."""
    c = _consent(account)
    v = c.get("latest_version") if "latest_version" in c else c.get("accepted_terms_version")
    return str(v) if isinstance(v, str) and v else None


def _rank(meta, version: Optional[str]) -> int:
    """Position in ``meta.terms_versions``; -1 for an unknown version (ranked below every known one)."""
    vs = _versions(meta)
    return vs.index(version) if version in vs else -1


def notice_needed(ctx) -> Optional[str]:
    """Which notice must be agreed before a run: ``FIRST``, ``UPDATED`` or None.  For the gate before run create."""
    meta, account = getattr(ctx, "meta", None) or {}, getattr(ctx, "account", None)
    if new_terms_mode(meta):
        latest = latest_accepted(account)
        if latest is None:
            return FIRST
        return UPDATED if _rank(meta, latest) < _rank(meta, NOTICE_TERMS_VERSION) else None
    accepted = _consent(account).get("accepted_terms_version")
    terms = meta.get("terms_version")
    if terms and accepted != terms:
        return UPDATED if accepted else FIRST
    return None


def notice_kind(ctx, legacy_updated: bool = False) -> str:
    """The variant the dialog shows.  New-terms mode decides from the account (whatever the caller asked for: a run
    refused with ``403 consent_required`` asks for the first notice even when the account agreed to an older version);
    legacy mode keeps the caller's choice."""
    meta = getattr(ctx, "meta", None) or {}
    if new_terms_mode(meta):
        latest = latest_accepted(getattr(ctx, "account", None))
        return UPDATED if latest is not None and _rank(meta, latest) < _rank(meta, NOTICE_TERMS_VERSION) else FIRST
    return UPDATED if legacy_updated else FIRST


def consent_version(ctx) -> Optional[str]:
    """The version "I agree" records: this build's notice version in new-terms mode, else the service's own (legacy)."""
    meta = getattr(ctx, "meta", None) or {}
    if new_terms_mode(meta):
        return NOTICE_TERMS_VERSION
    v = meta.get("terms_version")
    return str(v) if v else None


def run_create_fields(ctx) -> Dict[str, Any]:
    """Extra fields for ``POST /v1/runs``: ``{"terms_version": NOTICE_TERMS_VERSION}`` in new-terms mode, else none."""
    return {"terms_version": NOTICE_TERMS_VERSION} if new_terms_mode(getattr(ctx, "meta", None)) else {}
