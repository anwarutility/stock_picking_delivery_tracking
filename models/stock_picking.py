# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

import json
import logging
from datetime import datetime as dt, timezone

try:
    import requests
except ImportError:
    requests = None

from dateutil import parser as dateutil_parser

from odoo import _, fields, models
from odoo.exceptions import UserError

from ..services.tracking_api_client import (
    TrackingAPIClient,
)

FINAL_TRACKING_STATUSES = ["delivered", "exception"]

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = "stock.picking"

    delivery_tracking_status = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("shipped", "Shipped"),
            ("in_transit", "In Transit"),
            ("delivered", "Delivered"),
            ("exception", "Exception"),
            ("unknown", "Unknown"),
        ],
        string="Delivery Tracking Status",
        default="draft",
        copy=False,
        index=True,
    )
    delivery_tracking_status_desc = fields.Char(
        string="Status Description",
        copy=False,
    )
    delivery_last_check = fields.Datetime(
        string="Last Checked",
        copy=False,
    )
    delivery_estimated_date = fields.Datetime(
        string="Estimated Delivery Date",
    )
    delivery_tracking_error = fields.Char(
        string="Tracking Error",
        copy=False,
    )
    tracking_log_ids = fields.One2many(
        "delivery.tracking.log",
        "picking_id",
        string="Tracking History",
    )
    tracking_notification_sent = fields.Boolean(
        string="Tracking Notification Sent",
        default=False,
        copy=False,
        help="True once the salesperson has been notified by Telegram that "
        "the tracking reference was entered, so the notification is never "
        "sent twice for the same Delivery Order.",
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

    @staticmethod
    def _parse_event_datetime(raw_iso_string):
        """Convert an ISO-8601 string from the API into a UTC naive datetime.

        Returns ``False`` when parsing fails or input is empty so that bad
        timestamps never crash the tracking pipeline.
        """
        if not raw_iso_string:
            return False
        try:
            parsed = dateutil_parser.isoparse(raw_iso_string)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
        except (ValueError, TypeError) as exc:
            _logger.warning(
                "Delivery tracking: failed to parse event datetime '%s': %s",
                raw_iso_string,
                exc,
            )
            return False

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
        """Map API response to ``stock.picking`` fields and append new history logs."""
        self.ensure_one()
        vals = {
            "delivery_tracking_status": result.get("status") or "unknown",
            "delivery_tracking_status_desc": result.get("status_desc"),
            "delivery_last_check": fields.Datetime.now(),
        }
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

    def write(self, vals):
        """Trigger a one-time Telegram notification to the salesperson as
        soon as ``carrier_tracking_ref`` is entered.

        A failed Telegram send must never roll back / fail the picking
        write, so everything notification-related is wrapped in try/except.
        """
        res = super().write(vals)
        if "carrier_tracking_ref" in vals:
            icp = self.env["ir.config_parameter"].sudo()
            enabled = (
                icp.get_param(
                    "stock_picking_delivery_tracking.notify_sales_enabled",
                    default="True",
                )
                == "True"
            )
            if enabled:
                for picking in self:
                    if (
                        picking.carrier_tracking_ref
                        and not picking.tracking_notification_sent
                    ):
                        try:
                            picking._notify_sales_telegram()
                        except Exception:
                            _logger.exception(
                                "Delivery tracking Telegram notification "
                                "failed for %s",
                                picking.name,
                            )
        return res

    def _notify_sales_telegram(self):
        """Send the tracking-reference notification to the DO's salesperson.

        Sets ``tracking_notification_sent`` only after a successful send so
        that a transient failure can be retried manually.
        """
        self.ensure_one()
        salesperson = self.sale_id.user_id if self.sale_id else False
        if not salesperson or not salesperson.telegram_chat_id:
            _logger.info(
                "DO %s: salesperson has no telegram_chat_id, "
                "notification skipped",
                self.name,
            )
            return
        message = self._build_tracking_notification_message()
        try:
            self._send_telegram_message(salesperson.telegram_chat_id, message)
            self.tracking_notification_sent = True
        except Exception as exc:
            _logger.error(
                "Gagal kirim notifikasi Telegram DO %s: %s",
                self.name,
                exc,
            )

    def _build_tracking_notification_message(self):
        self.ensure_one()
        tracking_url = self.carrier_tracking_url or ""
        return _(
            "*DO %(name)s* sudah diinput resi\n"
            "Customer: %(customer)s\n"
            "Kurir: %(courier)s\n"
            "No. Resi: %(resi)s\n"
            "Link Tracking: %(url)s"
        ) % {
            "name": self.name or "-",
            "customer": self.partner_id.name or "-",
            "courier": self.carrier_id.name or "-",
            "resi": self.carrier_tracking_ref or "-",
            "url": tracking_url or "(belum tersedia)",
        }

    def _send_telegram_message(self, chat_id, message):
        token = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(
                "stock_picking_delivery_tracking.telegram_bot_token",
                default="",
            )
        )
        if not token:
            raise UserError(
                _("Telegram bot token belum dikonfigurasi di Settings")
            )
        if requests is None:
            raise RuntimeError(
                _("Library 'requests' tidak tersedia di server")
            )
        url = "https://api.telegram.org/bot%s/sendMessage" % token
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown",
        }
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return response.json()

    def action_resend_tracking_notification(self):
        """Manually resend the Telegram notification to the salesperson.

        Useful when the salesperson registered their ``telegram_chat_id``
        after the first attempt, or when a resend is needed.
        """
        for picking in self:
            if not picking.carrier_tracking_ref:
                raise UserError(
                    _("DO %s belum punya nomor resi.") % picking.name
                )
            picking._notify_sales_telegram()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Kirim Ulang Notifikasi"),
                "message": _("Notifikasi Telegram telah dikirim ulang."),
                "type": "info",
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
