# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

import logging
import time

import requests

_logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.biteship.com"

BiteshipStatusMap = {
    "confirmed": "shipped",
    "allocated": "shipped",
    "pickingup": "shipped",
    "picked": "shipped",
    "intransit": "in_transit",
    "droppingoff": "in_transit",
    "onhold": "in_transit",
    "delivered": "delivered",
    "returnintransit": "exception",
    "returned": "exception",
    "rejected": "exception",
    "couriernotfound": "exception",
    "cancelled": "exception",
    "disposed": "exception",
}


class TrackingAPIClient:
    """Pure-Python client for the Biteship Tracking API.

    The class does NOT depend on an Odoo environment; provider configuration is
    passed in as an object exposing at least ``api_base_url`` and ``api_key``
    attributes (typically a ``delivery.tracking.provider`` record). This keeps
    the class trivially unit-testable with plain Python objects.
    """

    def __init__(self, provider, timeout=10, retries=1, retry_delay=1):
        self.provider = provider
        self.timeout = timeout
        self.retries = max(retries, 0)
        self.retry_delay = retry_delay
        self.api_base_url = (
            getattr(provider, "api_base_url", None) or DEFAULT_BASE_URL
        ).rstrip("/")
        self.api_key = getattr(provider, "api_key", "") or ""
        self.auth_type = getattr(provider, "auth_type", None) or "api_key_header"
        self.auth_header_name = (
            getattr(provider, "auth_header_name", None) or "Authorization"
        )

    def _build_auth_header(self):
        """Return the authorization header for the configured auth type.

        Biteship uses raw API-key auth: the key is sent verbatim as the
        ``authorization`` header value (NOT ``Bearer <key>``). Other auth types
        are supported for forward compatibility with multi-provider setups.
        """
        value = self.api_key
        if self.auth_type == "bearer_token" and value and not value.startswith("Bearer "):
            value = "Bearer %s" % value
        return {self.auth_header_name: value}

    @staticmethod
    def _error_result(message, raw_response=None, error_code=None):
        return {
            "status": "unknown",
            "status_desc": None,
            "location": None,
            "event_datetime": None,
            "history": [],
            "error": message,
            "error_code": error_code,
            "raw_response": raw_response,
        }

    @classmethod
    def _map_status(cls, biteship_status):
        """Map a Biteship status string to the internal Odoo status.

        Mapping is case-insensitive. Any status not in the official list falls
        back to ``unknown``.
        """
        if not biteship_status:
            return "unknown"
        return BiteshipStatusMap.get(biteship_status.lower(), "unknown")

    def get_couriers(self):
        """Fetch the list of supported couriers from ``GET /v1/couriers``.

        Optional helper intended for re-verification when Biteship adds or
        renames couriers. The confirmed courier codes (jne, jnt, lion, wahana,
        pos) should be set directly on ``delivery.carrier.biteship_courier_code``
        and do not require calling this endpoint at setup time.
        """
        url = "%s/v1/couriers" % self.api_base_url
        try:
            response = requests.get(
                url,
                headers=self._build_auth_header(),
                timeout=self.timeout,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            _logger.error("Biteship get_couriers failed: %s", exc)
            return []
        if response.status_code != 200:
            _logger.warning(
                "Biteship get_couriers returned HTTP %s: %s",
                response.status_code,
                response.text[:500],
            )
            return []
        try:
            data = response.json()
        except ValueError as exc:
            _logger.error("Biteship get_couriers invalid JSON: %s", exc)
            return []
        return data.get("couriers") or []

    def get_status(self, waybill_id, courier_code):
        """Track a waybill and return a normalized status dict.

        Calls ``GET /v1/trackings/{waybill_id}/couriers/{courier_code}``.
        Never raises: every failure path is logged and returned as a dict with
        ``status='unknown'`` so the calling cron/action never crashes.
        """
        if not waybill_id or not courier_code:
            return self._error_result(
                "missing waybill_id or courier_code",
                error_code="invalid_arguments",
            )
        url = "%s/v1/trackings/%s/couriers/%s" % (
            self.api_base_url,
            waybill_id,
            courier_code,
        )
        headers = self._build_auth_header()
        for attempt in range(self.retries + 1):
            try:
                response = requests.get(
                    url,
                    headers=headers,
                    timeout=self.timeout,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                _logger.warning(
                    "Biteship get_status network error (%s, attempt %d/%d): %s",
                    waybill_id,
                    attempt + 1,
                    self.retries + 1,
                    exc,
                )
                if attempt < self.retries:
                    time.sleep(self.retry_delay)
                    continue
                return self._error_result(
                    "network error: %s" % exc,
                    error_code="network_error",
                )
            if response.status_code >= 500:
                _logger.warning(
                    "Biteship get_status server error HTTP %s (attempt %d/%d): %s",
                    response.status_code,
                    attempt + 1,
                    self.retries + 1,
                    response.text[:500],
                )
                if attempt < self.retries:
                    time.sleep(self.retry_delay)
                    continue
                return self._error_result(
                    "server error HTTP %s" % response.status_code,
                    raw_response=response.text,
                    error_code="http_5xx",
                )
            if response.status_code in (401, 403):
                _logger.error(
                    "Biteship get_status authentication failed HTTP %s for %s",
                    response.status_code,
                    waybill_id,
                )
                return self._error_result(
                    "authentication failed HTTP %s" % response.status_code,
                    raw_response=response.text,
                    error_code="auth_error",
                )
            if response.status_code != 200:
                _logger.warning(
                    "Biteship get_status unexpected HTTP %s for %s: %s",
                    response.status_code,
                    waybill_id,
                    response.text[:500],
                )
                error_message = "HTTP %s" % response.status_code
                if response.text:
                    try:
                        body = response.json()
                    except ValueError:
                        body = None
                    if isinstance(body, dict):
                        detail = body.get("error") or body.get("message")
                        if detail:
                            error_message = "%s: %s" % (error_message, detail)
                return self._error_result(
                    error_message,
                    raw_response=response.text,
                    error_code="http_error",
                )
            break

        try:
            data = response.json()
        except ValueError as exc:
            _logger.error(
                "Biteship get_status invalid JSON for %s: %s",
                waybill_id,
                exc,
            )
            return self._error_result(
                "invalid JSON response: %s" % exc,
                raw_response=response.text,
                error_code="parse_error",
            )
        if not data.get("success"):
            message = data.get("message") or data.get("error") or "success=false"
            _logger.warning(
                "Biteship get_status returned success=false for %s: %s",
                waybill_id,
                message,
            )
            return self._error_result(
                "api error: %s" % message,
                raw_response=data,
                error_code="api_error",
            )
        return self._normalize_success(data)

    def _normalize_success(self, data):
        history = data.get("history") or []
        status_desc = None
        event_datetime = None
        if history:
            last_event = history[-1]
            status_desc = last_event.get("note")
            event_datetime = last_event.get("updated_at")
        biteship_status = data.get("status")
        if not biteship_status and history:
            biteship_status = history[-1].get("status")
        return {
            "status": self._map_status(biteship_status),
            "status_desc": status_desc,
            "location": None,
            "event_datetime": event_datetime,
            "history": history,
            "raw_response": data,
        }
