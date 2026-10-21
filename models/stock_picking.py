# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

import json
import logging
import uuid
from datetime import datetime as dt, timezone

try:
    import requests
except ImportError:
    requests = None

from dateutil import parser as dateutil_parser

from odoo import _, api, fields, models
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

# Milestones shown on the customer-facing tracking portal. Derived from the
# internal status + Biteship history (see ``_get_delivery_tracking_steps``).
DELIVERY_TRACKING_STEPS = [
    ("order_created", "Order Dibuat"),
    ("in_progress", "Diproses"),
    ("shipped", "Dikirim"),
    ("in_transit", "Dalam Perjalanan"),
    ("out_for_delivery", "Diantar"),
    ("delivered", "Selesai"),
]

# Customer-friendly labels for the granular status history (portal only).
DELIVERY_TRACKING_STATUS_LABELS = {
    "draft": "Draft",
    "shipped": "Dikirim",
    "in_transit": "Dalam Perjalanan",
    "delivered": "Selesai",
    "exception": "Kendala / Retur",
    "unknown": "Belum Diketahui",
}


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
    delivery_tracking_token = fields.Char(
        string="Delivery Tracking Token",
        copy=False,
        default=lambda self: uuid.uuid4().hex,
        help="Secret token used in the public no-login tracking URL. "
        "Regenerated whenever the carrier or the tracking reference changes.",
    )
    delivery_tracking_url = fields.Char(
        string="Public Tracking URL",
        compute="_compute_delivery_tracking_url",
        help="Public no-login URL that lets the customer follow the shipment.",
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

    @api.depends("delivery_tracking_token")
    def _compute_delivery_tracking_url(self):
        """Public no-login tracking URL built from the secret token."""
        for picking in self:
            token = picking.delivery_tracking_token
            picking.delivery_tracking_url = (
                "%s/tracking/%s" % (picking.get_base_url(), token) if token else False
            )

    def _ensure_delivery_tracking_token(self):
        """Set a fresh secret token on pickings that do not have one yet.

        Tokens are generated automatically on create, but this covers legacy
        records (imported before the token field existed) and any record whose
        token was manually cleared.
        """
        for picking in self:
            if not picking.delivery_tracking_token:
                picking.delivery_tracking_token = uuid.uuid4().hex
        return self

    def _format_tracking_datetime(self, value):
        """Format a naive-UTC datetime for the portal in the company timezone."""
        if not value:
            return False
        try:
            localized = fields.Datetime.context_timestamp(self, value)
            return localized.strftime("%d/%m/%Y %H:%M")
        except Exception:
            return fields.Datetime.to_string(value)

    def _format_tracking_date(self, value):
        """Format a naive-UTC datetime for the portal (date only)."""
        if not value:
            return False
        try:
            localized = fields.Datetime.context_timestamp(self, value)
            return localized.strftime("%d/%m/%Y")
        except Exception:
            return fields.Datetime.to_string(value)

    def _tracking_status_display_label(self, status):
        return DELIVERY_TRACKING_STATUS_LABELS.get(status, status or "")

    def _tracking_log_min_ts(self, log_filter):
        """Earliest event_datetime of the matching tracking logs (or False)."""
        ts = [
            log.event_datetime
            for log in self.tracking_log_ids
            if log_filter(log) and log.event_datetime
        ]
        return min(ts) if ts else False

    def _get_delivery_tracking_steps(self):
        """Return the ordered timeline milestones for the portal page.

        Each dict has: key, label, reached, current, ts and ts_display.
        ``current`` is the first milestone not reached yet (the one being
        worked on) — or the last milestone once everything is reached. On an
        exception the milestone flow is interrupted (no current step) and the
        caller renders a dedicated exception banner instead.
        """
        self.ensure_one()
        status = self.delivery_tracking_status
        date_done = self.date_done or False

        shipped_ts = (
            self._tracking_log_min_ts(lambda log: log.status == "shipped")
            or date_done
        )
        in_transit_ts = self._tracking_log_min_ts(
            lambda log: log.status == "in_transit"
        )
        delivered_ts = self._tracking_log_min_ts(
            lambda log: log.status == "delivered"
        )

        # "Diantar" (out for delivery) is not an internal status: Biteship
        # reports it as ``droppingoff`` which maps to ``in_transit``. Detect
        # it from the raw history payload stored on the log rows.
        out_for_delivery_ts = False
        for log in self.tracking_log_ids:
            raw_status = None
            if log.raw_response:
                try:
                    raw = json.loads(log.raw_response)
                except (ValueError, TypeError):
                    raw = None
                if isinstance(raw, dict):
                    raw_status = raw.get("status")
            if raw_status and str(raw_status).lower() in (
                "droppingoff",
                "dropping_off",
            ):
                ts = log.event_datetime
                if ts and (not out_for_delivery_ts or ts < out_for_delivery_ts):
                    out_for_delivery_ts = ts

        reached = {
            "order_created": True,
            "in_progress": self.state in ("assigned", "done"),
            "shipped": (
                self.state == "done"
                or status in ("shipped", "in_transit", "delivered")
                or bool(shipped_ts)
            ),
            "in_transit": status in ("in_transit", "delivered") or bool(in_transit_ts),
            "out_for_delivery": (
                bool(out_for_delivery_ts) or status == "delivered" or bool(delivered_ts)
            ),
            "delivered": status == "delivered" or bool(delivered_ts),
        }
        timestamps = {
            "order_created": self.create_date,
            "in_progress": self.date or self.create_date,
            "shipped": shipped_ts,
            "in_transit": in_transit_ts or shipped_ts,
            "out_for_delivery": out_for_delivery_ts or delivered_ts or shipped_ts,
            "delivered": delivered_ts or self.delivery_last_check,
        }

        steps = []
        for key, label in DELIVERY_TRACKING_STEPS:
            reached_flag = bool(reached.get(key))
            ts = timestamps.get(key)
            steps.append(
                {
                    "key": key,
                    "label": label,
                    "reached": reached_flag,
                    "current": False,
                    "ts": ts,
                    "ts_display": self._format_tracking_datetime(ts),
                }
            )

        if not reached.get("delivered") and status != "exception":
            current_index = next(
                (i for i, step in enumerate(steps) if not step["reached"]), 0
            )
            steps[current_index]["current"] = True
        elif reached.get("delivered"):
            steps[-1]["current"] = True
        return steps

    def _get_delivery_tracking_data(self):
        """Build the full values dict rendered by the portal tracking page.

        Only tracking-related fields are exposed: this is also used by the
        no-login public token route, so no price / other order data leaks.
        """
        self.ensure_one()
        status = self.delivery_tracking_status
        exception = False
        if status == "exception":
            exception = {
                "desc": self.delivery_tracking_status_desc,
                "error": self.delivery_tracking_error,
            }
        history = [
            {
                "status": log.status,
                "status_label": self._tracking_status_display_label(log.status),
                "desc": log.status_desc,
                "location": log.location,
                "ts_display": self._format_tracking_datetime(log.event_datetime),
            }
            for log in self.tracking_log_ids.sorted(
                key=lambda log: log.event_datetime or dt.min
            )
        ]
        return {
            "picking": self,
            "picking_name": self.name or "-",
            "partner_name": self.partner_id.name or "-",
            "resi": self.carrier_tracking_ref or False,
            "courier_name": self.carrier_id.name or "-",
            "eta_display": self._format_tracking_date(self.delivery_estimated_date),
            "last_check_display": self._format_tracking_datetime(
                self.delivery_last_check
            ),
            "status": status,
            "current_status": {
                "label": self._tracking_status_display_label(status),
                "desc": self.delivery_tracking_status_desc,
            },
            "steps": self._get_delivery_tracking_steps(),
            "exception": exception,
            "unknown": status == "unknown",
            "tracking_url": self.delivery_tracking_url,
            "history": history,
            "is_public": False,
        }

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

    def write(self, vals):
        """Trigger a one-time Telegram notification to the salesperson as
        soon as ``carrier_tracking_ref`` is entered, and reset the tracking
        state whenever the carrier or the tracking reference changes.

        A failed Telegram send must never roll back / fail the picking
        write, so everything notification-related is wrapped in try/except.
        """
        changed = "carrier_tracking_ref" in vals or "carrier_id" in vals
        previous = {}
        if changed:
            # Snapshot the OLD values BEFORE the write: ``picking._origin``
            # points to the same record (same cache), so it cannot be used
            # to detect a real change after ``super().write()``.
            previous = {
                picking.id: (picking.carrier_tracking_ref, picking.carrier_id)
                for picking in self
            }
        res = super().write(vals)
        if changed:
            for picking in self:
                old_ref, old_carrier = previous[picking.id]
                new_ref = picking.carrier_tracking_ref
                new_carrier = picking.carrier_id
                if old_ref != new_ref or old_carrier != new_carrier:
                    picking.write({
                        "delivery_tracking_status": "draft",
                        "delivery_tracking_status_desc": False,
                        "delivery_tracking_error": False,
                        "delivery_last_check": False,
                        "delivery_tracking_token": uuid.uuid4().hex,
                    })
                    picking.tracking_notification_sent = False
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
        tracking_url = self.delivery_tracking_url or self.carrier_tracking_url or ""
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
