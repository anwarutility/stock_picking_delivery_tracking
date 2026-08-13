# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

import json
import logging
from datetime import datetime as dt, timezone

from dateutil import parser as dateutil_parser

from odoo import _, fields, models
from odoo.exceptions import UserError

from odoo.addons.stock_picking_delivery_tracking.services.tracking_api_client import (
    TrackingAPIClient,
)

_logger = logging.getLogger(__name__)

TRACKING_STATUS_SELECTION = [
    ("draft", "Draft"),
    ("shipped", "Shipped"),
    ("in_transit", "In Transit"),
    ("delivered", "Delivered"),
    ("exception", "Exception"),
    ("unknown", "Unknown"),
]

# Statuses considered "final": the cron will stop polling pickings in these.
FINAL_TRACKING_STATUSES = ["delivered", "exception"]


class StockPicking(models.Model):
    _inherit = "stock.picking"

    delivery_tracking_status = fields.Selection(
        TRACKING_STATUS_SELECTION,
        string="Delivery Tracking Status",
        default="draft",
    )
    delivery_tracking_status_desc = fields.Char(
        string="Delivery Status Description",
    )
    delivery_last_check = fields.Datetime(string="Last Check")
    delivery_estimated_date = fields.Datetime(string="Estimated Delivery Date")
    delivery_tracking_error = fields.Char(
        string="Tracking Error",
        help="Pesan error dari provider saat percobaan tracking terakhir "
        "gagal. Terisi hanya jika status menjadi 'unknown'.",
    )
    tracking_log_ids = fields.One2many(
        "delivery.tracking.log",
        "picking_id",
        string="Tracking History",
    )

    def _get_active_tracking_provider(self):
        """Return the first active ``delivery.tracking.provider``, or empty set.

        Logs a warning when no active provider exists so the caller can skip
        the affected records instead of crashing the cron.
        """
        providers = self.env["delivery.tracking.provider"].search(
            [("active", "=", True)]
        )
        if not providers:
            _logger.warning(
                "Delivery tracking: no active delivery.tracking.provider "
                "configured, skipping tracking."
            )
            return self.env["delivery.tracking.provider"]
        if len(providers) > 1:
            _logger.warning(
                "Delivery tracking: %d active providers found, using '%s'.",
                len(providers),
                providers[0].name,
            )
        return providers[0]

    def _parse_event_datetime(self, value):
        """Parse an API timestamp into a naive UTC datetime.

        Biteship ``updated_at`` uses ISO-8601 (``T`` separator, optional UTC
        offset), which ``fields.Datetime.to_datetime`` cannot parse directly.
        Returns ``None`` when the value is unparseable.
        """
        if not value:
            return None
        try:
            parsed = dateutil_parser.isoparse(str(value))
        except (ValueError, TypeError):
            try:
                parsed = dateutil_parser.parse(str(value))
            except (ValueError, TypeError, OverflowError):
                _logger.debug("Unparseable tracking timestamp: %r", value)
                return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    def _extract_estimated_date(self, result):
        """Best-effort estimated delivery date from the raw Biteship response.

        Biteship does not document an ETD field for the tracking endpoint, so
        common variants are checked defensively. Returns ``False`` when absent.
        """
        raw = result.get("raw_response")
        if not isinstance(raw, dict):
            return False
        for key in ("estimated_delivery_date", "estimated_delivery", "etd"):
            value = raw.get(key)
            if value:
                parsed = self._parse_event_datetime(value)
                if parsed:
                    return parsed
        return False

    def _process_tracking_result(self, result):
        """Apply a normalized tracking result to this picking.

        - Always updates ``delivery_last_check`` (even on error) so failing
          pickings are not retried on every poll.
        - Creates one ``delivery.tracking.log`` per history entry whose event
          datetime is not yet recorded (prevents duplicate logs each poll).
        """
        self.ensure_one()
        now = fields.Datetime.now()
        vals = {
            "delivery_last_check": now,
            "delivery_tracking_status": result.get("status") or "unknown",
            "delivery_tracking_status_desc": result.get("status_desc"),
        }
        # Persist the provider error whenever the check did not yield a real
        # status, so warehouse staff can see WHY a picking is stuck on Unknown
        # without digging into logs or a shell.
        error_message = result.get("error")
        if error_message and result.get("status") == "unknown":
            vals["delivery_tracking_error"] = error_message
        else:
            vals["delivery_tracking_error"] = False
        estimated_date = self._extract_estimated_date(result)
        if estimated_date:
            vals["delivery_estimated_date"] = estimated_date
        self.write(vals)

        existing_dates = {
            ts
            for ts in self.tracking_log_ids.mapped("event_datetime")
            if ts
        }
        for item in result.get("history") or []:
            event_datetime = self._parse_event_datetime(item.get("updated_at"))
            if not event_datetime or event_datetime in existing_dates:
                continue
            self.env["delivery.tracking.log"].create(
                {
                    "picking_id": self.id,
                    "status": TrackingAPIClient._map_status(item.get("status")),
                    "status_desc": item.get("note"),
                    "location": item.get("location"),
                    "event_datetime": event_datetime,
                    "raw_response": json.dumps(item),
                }
            )
            existing_dates.add(event_datetime)

    def _track_single(self, provider=None):
        """Fetch and process tracking status for this picking.

        Returns ``True`` when the status was refreshed, ``False`` when the
        record was skipped (unmapped courier, missing resi, or no provider).
        """
        self.ensure_one()
        if not self.carrier_tracking_ref:
            _logger.warning(
                "Delivery tracking: picking %s has no carrier tracking ref.",
                self.name,
            )
            return False
        if not self.carrier_id.biteship_courier_code:
            _logger.warning(
                "Delivery tracking: carrier '%s' on picking %s is not mapped "
                "to a Biteship courier code.",
                self.carrier_id.name,
                self.name,
            )
            return False
        if not provider:
            provider = self._get_active_tracking_provider()
        if not provider:
            return False
        client = TrackingAPIClient(provider)
        result = client.get_status(
            self.carrier_tracking_ref, self.carrier_id.biteship_courier_code
        )
        self._process_tracking_result(result)
        return True

    def cron_check_delivery_status(self):
        """Poll tracking status for eligible outgoing done pickings.

        Batches the run (default 50, configurable via res.config.settings) and
        processes oldest-checked first so long-stale pickings get priority.
        """
        provider = self._get_active_tracking_provider()
        if not provider:
            return False
        icp = self.env["ir.config_parameter"]
        batch_size = int(
            icp.get_param(
                "delivery_tracking_cron_batch_size",
                default=50,
            )
        )
        domain = [
            ("picking_type_code", "=", "outgoing"),
            ("state", "=", "done"),
            ("carrier_tracking_ref", "!=", False),
            ("carrier_tracking_ref", "!=", ""),
            ("delivery_tracking_status", "not in", FINAL_TRACKING_STATUSES),
        ]
        pickings = self.search(domain)
        # Oldest-checked first; never-checked (NULL last_check) get priority.
        pickings = pickings.sorted(
            key=lambda p: p.delivery_last_check or dt.min,
            reverse=False,
        )[:batch_size]
        for picking in pickings:
            picking._track_single(provider)
        return True

    def action_check_delivery_status(self):
        """Manually refresh tracking status for the current record(s).

        Called from the "Cek Status Pengiriman" button on the Delivery form.
        Shows a notification summarising the outcome instead of failing
        silently.
        """
        for picking in self:
            if not picking.carrier_id.biteship_courier_code:
                raise UserError(
                    _(
                        "Courier '%s' is not mapped to a Biteship courier code. "
                        "Please fill the 'Biteship Courier Code' field on the "
                        "carrier (e.g. jne, jnt, lion, wahana, pos)."
                    )
                    % picking.carrier_id.name
                )

        refreshed = 0
        skipped = 0
        for picking in self:
            if picking._track_single():
                refreshed += 1
            else:
                skipped += 1

        if refreshed:
            message = _(
                "Status pengiriman berhasil diperbarui untuk %s DO."
            ) % refreshed
            notification_type = "success"
        else:
            message = _(
                "Tidak ada status yang diperbarui (%s DO dilewati). "
                "Periksa apakah resi terisi dan kurir sudah di-mapping ke Biteship."
            ) % skipped
            notification_type = "warning"

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Cek Status Pengiriman"),
                "message": message,
                "type": notification_type,
                "sticky": False,
            },
        }

    def action_refresh_selected_delivery_status(self):
        """Bulk refresh tracking status for selected Delivery Orders.

        Bound as a server action on the list/kanban views. Guards against
        firing more than 20 API calls per invocation and aggregates the
        outcome (success/failure) into a single notification.
        """
        MAX_BULK = 20
        if len(self) > MAX_BULK:
            raise UserError(
                _("Maksimal %s DO sekaligus, Anda memilih %s.")
                % (MAX_BULK, len(self))
            )

        refreshed = 0
        failed = 0
        failed_reasons = []
        for picking in self:
            try:
                if picking._track_single():
                    refreshed += 1
                else:
                    failed += 1
                    if not picking.carrier_tracking_ref:
                        reason = _("%s: nomor resi kosong") % picking.name
                    elif not picking.carrier_id.biteship_courier_code:
                        reason = _("%s: kurir '%s' belum di-mapping Biteship") % (
                            picking.name,
                            picking.carrier_id.name,
                        )
                    else:
                        reason = _("%s: tidak ada provider aktif") % picking.name
                    failed_reasons.append(reason)
            except Exception as exc:  # never crash the whole bulk run
                _logger.exception(
                    "Delivery tracking bulk refresh failed for %s", picking.name
                )
                failed += 1
                failed_reasons.append(_("%s: %s") % (picking.name, exc))

        if failed:
            message = _(
                "%s berhasil diperbarui, %s gagal. "
                "Rincian: %s"
            ) % (
                refreshed,
                failed,
                "; ".join(failed_reasons),
            )
            notification_type = "warning"
        else:
            message = _("%s status berhasil diperbarui.") % refreshed
            notification_type = "success"

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Refresh Status Terpilih"),
                "message": message,
                "type": notification_type,
                "sticky": False,
            },
        }
