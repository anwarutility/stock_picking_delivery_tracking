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
FINAL_TRACKING_STATUSES = ["delivered", "exception", "shipped"]

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

    # =========================================================================
    # CARRIER 1 TRACKING FIELDS
    # =========================================================================
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
    biteship_tracking_id = fields.Char(
        string="Biteship Tracking ID",
        copy=False,
        help="Stable Biteship tracking object id returned by the status API. "
        "Used to build the public tracking link shown in Telegram messages.",
    )
    delivery_tracking_retry_count = fields.Integer(
        string="Tracking Retry Count Kurir 1",
        default=0,
        copy=False,
    )

    # =========================================================================
    # CARRIER 2 TRACKING FIELDS
    # =========================================================================
    carrier_id_2 = fields.Many2one(
        "delivery.carrier",
        string="Kurir 2",
        copy=True,
        help="Kurir kedua (pengiriman lanjutan / estafet).",
    )
    carrier_tracking_ref_2 = fields.Char(
        string="No. Resi 2",
        copy=True,
        help="Nomor resi kurir kedua.",
    )
    delivery_tracking_status_2 = fields.Selection(
        TRACKING_STATUS_SELECTION,
        string="Status Tracking Kurir 2",
        default="draft",
    )
    delivery_tracking_status_desc_2 = fields.Char(
        string="Keterangan Status Kurir 2",
    )
    delivery_last_check_2 = fields.Datetime(string="Last Check Kurir 2")
    delivery_estimated_date_2 = fields.Datetime(string="Estimasi Tiba Kurir 2")
    delivery_tracking_error_2 = fields.Char(
        string="Tracking Error Kurir 2",
    )
    biteship_tracking_id_2 = fields.Char(
        string="Biteship Tracking ID Kurir 2",
        copy=False,
    )
    delivery_tracking_token_2 = fields.Char(
        string="Delivery Tracking Token 2",
        copy=False,
        default=lambda self: uuid.uuid4().hex,
        help="Secret token used in the public no-login tracking URL for Carrier 2.",
    )
    delivery_tracking_url_2 = fields.Char(
        string="Public Tracking URL 2",
        compute="_compute_delivery_tracking_url_2",
        help="Public no-login URL that lets the customer follow the shipment of Carrier 2.",
    )
    delivery_tracking_retry_count_2 = fields.Integer(
        string="Tracking Retry Count Kurir 2",
        default=0,
        copy=False,
    )

    # =========================================================================
    # COMMON FIELDS
    # =========================================================================
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

    def init(self):
        super().init()
        self.env.cr.execute(
            """
            UPDATE stock_picking
            SET delivery_tracking_token = md5(random()::text || clock_timestamp()::text)
            WHERE delivery_tracking_token IS NULL;

            UPDATE stock_picking
            SET delivery_tracking_token_2 = md5(random()::text || clock_timestamp()::text)
            WHERE delivery_tracking_token_2 IS NULL;
            """
        )

    @api.depends("delivery_tracking_token")
    def _compute_delivery_tracking_url(self):
        """Public no-login tracking URL built from the secret token."""
        for picking in self:
            token = picking.delivery_tracking_token
            if not token:
                token = uuid.uuid4().hex
                picking.delivery_tracking_token = token
            base_url = picking.get_base_url()
            if base_url.startswith("http://") and "orimax.co.id" in base_url:
                base_url = "https://" + base_url[len("http://") :]
            picking.delivery_tracking_url = "%s/tracking/%s" % (base_url, token)

    @api.depends("delivery_tracking_token_2")
    def _compute_delivery_tracking_url_2(self):
        """Public no-login tracking URL 2 built from the secret token 2."""
        for picking in self:
            token = picking.delivery_tracking_token_2
            if not token:
                token = uuid.uuid4().hex
                picking.delivery_tracking_token_2 = token
            base_url = picking.get_base_url()
            if base_url.startswith("http://") and "orimax.co.id" in base_url:
                base_url = "https://" + base_url[len("http://") :]
            picking.delivery_tracking_url_2 = "%s/tracking/%s" % (base_url, token)

    def _ensure_delivery_tracking_token(self):
        """Set a fresh secret token on pickings that do not have one yet.

        Tokens are generated automatically on create, but this covers legacy
        records (imported before the token field existed) and any record whose
        token was manually cleared.
        """
        for picking in self:
            if not picking.delivery_tracking_token:
                picking.delivery_tracking_token = uuid.uuid4().hex
            if not picking.delivery_tracking_token_2:
                picking.delivery_tracking_token_2 = uuid.uuid4().hex
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
        """Format a naive-UTC datetime for the portal (date only, e.g. 29 Okt 2026)."""
        if not value:
            return False
        try:
            localized = fields.Datetime.context_timestamp(self, value)
            months = [
                "", "Jan", "Feb", "Mar", "Apr", "Mei", "Jun",
                "Jul", "Agu", "Sep", "Okt", "Nov", "Des"
            ]
            m = months[localized.month] if 1 <= localized.month <= 12 else str(localized.month)
            return localized.strftime(f"%d {m} %Y")
        except Exception:
            return fields.Datetime.to_string(value)

    def _format_tracking_time(self, value):
        """Format a naive-UTC datetime for the portal (time only, e.g. 10:15 WIB)."""
        if not value:
            return False
        try:
            localized = fields.Datetime.context_timestamp(self, value)
            return localized.strftime("%H:%M") + " WIB"
        except Exception:
            return False

    def _get_tracking_event_icon(self, status, title=""):
        """Return a FontAwesome icon class matching the milestone or event status."""
        s = (status or "").lower()
        t = (title or "").lower()
        if "selesai" in t or "diterima" in t or s == "delivered":
            return "fa-check-circle text-success"
        if "antar" in t or "out_for_delivery" in s:
            return "fa-motorcycle text-primary"
        if "jalan" in t or "transit" in s:
            return "fa-truck text-primary"
        if "resi" in t or s == "shipped":
            return "fa-barcode text-info"
        if "kemas" in t or "siap" in t or s in ("in_progress", "ready"):
            return "fa-archive text-warning"
        if "dibuat" in t or s == "draft":
            return "fa-file-text-o text-muted"
        if "kendala" in t or s == "exception":
            return "fa-exclamation-triangle text-danger"
        return "fa-circle-o text-muted"

    def _get_carrier_brand_info(self, carrier_obj):
        """Return courier brand colors, styling class, and clean display name."""
        code = ""
        name = ""
        if carrier_obj:
            code = (getattr(carrier_obj, "biteship_courier_code", "") or "").lower()
            name = carrier_obj.name or ""
        n_low = name.lower()

        if "j&t cargo" in n_low or "jnt cargo" in n_low or "jntcargo" in code:
            return {"brand_class": "carrier-jntcargo", "badge_label": "J&T Cargo", "code": "jntcargo", "color": "#ED1C24"}
        elif "j&t" in n_low or "jnt" in code:
            return {"brand_class": "carrier-jnt", "badge_label": "J&T Express", "code": "jnt", "color": "#ED1C24"}
        elif "jne" in n_low or "jne" in code:
            return {"brand_class": "carrier-jne", "badge_label": "JNE Express", "code": "jne", "color": "#003366"}
        elif "sicepat" in n_low or "sicepat" in code:
            return {"brand_class": "carrier-sicepat", "badge_label": "SiCepat", "code": "sicepat", "color": "#CD0000"}
        elif "anteraja" in n_low or "anteraja" in code:
            return {"brand_class": "carrier-anteraja", "badge_label": "Anteraja", "code": "anteraja", "color": "#D81A60"}
        elif "pos" in n_low or "pos" in code:
            return {"brand_class": "carrier-pos", "badge_label": "POS Indonesia", "code": "pos", "color": "#FF6600"}
        elif "tiki" in n_low or "tiki" in code:
            return {"brand_class": "carrier-tiki", "badge_label": "TIKI", "code": "tiki", "color": "#003B70"}
        elif "ninja" in n_low or "ninja" in code:
            return {"brand_class": "carrier-ninja", "badge_label": "Ninja Xpress", "code": "ninja", "color": "#C41230"}
        elif "lion" in n_low or "lion" in code:
            return {"brand_class": "carrier-lion", "badge_label": "Lion Parcel", "code": "lion", "color": "#E61C24"}
        elif "spx" in n_low or "shopee" in n_low:
            return {"brand_class": "carrier-spx", "badge_label": "SPX Express", "code": "spx", "color": "#EE4D2D"}
        return {"brand_class": "carrier-generic", "badge_label": name or "Ekspedisi", "code": "generic", "color": "#2563EB"}

    def _tracking_status_display_label(self, status):
        return DELIVERY_TRACKING_STATUS_LABELS.get(status, status or "")

    def _get_delivery_tracking_steps(self, carrier_num=1):
        """Return the ordered timeline milestones for the portal page.

        Each dict has: key, label, reached, current, ts and ts_display.
        ``current`` is the first milestone not reached yet (the one being
        worked on) — or the last milestone once everything is reached.
        """
        self.ensure_one()
        carrier_obj = self.carrier_id if carrier_num == 1 else self.carrier_id_2
        resi = self.carrier_tracking_ref if carrier_num == 1 else self.carrier_tracking_ref_2
        has_resi = bool(resi and str(resi).strip())
        status = (
            self.delivery_tracking_status
            if carrier_num == 1
            else self.delivery_tracking_status_2
        )
        date_done = self.date_done or False

        carrier_logs = self.tracking_log_ids.filtered(
            lambda l: not l.carrier_id or l.carrier_id == carrier_obj
        )

        def log_min_ts(log_filter):
            ts = [
                l.event_datetime
                for l in carrier_logs
                if log_filter(l) and l.event_datetime
            ]
            return min(ts) if ts else False

        shipped_ts = log_min_ts(lambda l: l.status == "shipped") or (date_done if has_resi else False)
        in_transit_ts = log_min_ts(lambda l: l.status == "in_transit")
        delivered_ts = log_min_ts(lambda l: l.status == "delivered")

        # "Diantar" (out for delivery) detected from raw history payload
        out_for_delivery_ts = False
        for log in carrier_logs:
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

        if not has_resi:
            # Jika nomor resi belum diinput: barang masih diproses / disiapkan di gudang.
            # Status 'Dikirim' belum dicentang, dan status yang aktif adalah 'Diproses'.
            reached = {
                "order_created": True,
                "in_progress": False,
                "shipped": False,
                "in_transit": False,
                "out_for_delivery": False,
                "delivered": False,
            }
        else:
            reached = {
                "order_created": True,
                "in_progress": True,
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

        last_check = (
            self.delivery_last_check
            if carrier_num == 1
            else self.delivery_last_check_2
        )
        timestamps = {
            "order_created": self.create_date,
            "in_progress": self.date or self.create_date,
            "shipped": shipped_ts if reached["shipped"] else False,
            "in_transit": in_transit_ts if reached["in_transit"] else False,
            "out_for_delivery": out_for_delivery_ts if reached["out_for_delivery"] else False,
            "delivered": delivered_ts or (last_check if reached["delivered"] else False),
        }

        steps = []
        for key, label in DELIVERY_TRACKING_STEPS:
            reached_flag = bool(reached.get(key))
            ts = timestamps.get(key)
            ts_to_display = ts if (reached_flag or key in ("order_created", "in_progress")) else False
            steps.append(
                {
                    "key": key,
                    "label": label,
                    "reached": reached_flag,
                    "current": False,
                    "ts": ts_to_display,
                    "ts_display": self._format_tracking_datetime(ts_to_display) if ts_to_display else False,
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

    def _get_delivery_tracking_data(self, carrier_num=1):
        """Build the values dict rendered by the portal tracking page for
        either Carrier 1 or Carrier 2.
        """
        self.ensure_one()
        if carrier_num == 2 and self.carrier_id_2:
            status = self.delivery_tracking_status_2 or "draft"
            status_desc = self.delivery_tracking_status_desc_2
            status_error = self.delivery_tracking_error_2
            courier_name = self.carrier_id_2.name or "-"
            resi = self.carrier_tracking_ref_2 or False
            eta = self.delivery_estimated_date_2
            last_check = self.delivery_last_check_2
            tracking_url = self.delivery_tracking_url_2
            carrier_obj = self.carrier_id_2
            carrier_label = "Kurir 2"
            other_carrier = (
                {
                    "carrier_num": 1,
                    "name": self.carrier_id.name or "-",
                    "resi": self.carrier_tracking_ref or "-",
                    "url": self.delivery_tracking_url,
                    "label": "Kurir 1",
                }
                if self.carrier_tracking_ref
                else False
            )
        else:
            carrier_num = 1
            status = self.delivery_tracking_status or "draft"
            status_desc = self.delivery_tracking_status_desc
            status_error = self.delivery_tracking_error
            courier_name = self.carrier_id.name or "-"
            resi = self.carrier_tracking_ref or False
            eta = self.delivery_estimated_date
            last_check = self.delivery_last_check
            tracking_url = self.delivery_tracking_url
            carrier_obj = self.carrier_id
            carrier_label = "Kurir 1" if self.carrier_tracking_ref_2 else False
            other_carrier = (
                {
                    "carrier_num": 2,
                    "name": self.carrier_id_2.name or "-",
                    "resi": self.carrier_tracking_ref_2 or "-",
                    "url": self.delivery_tracking_url_2,
                    "label": "Kurir 2",
                }
                if self.carrier_tracking_ref_2
                else False
            )

        exception = False
        if status == "exception":
            exception = {
                "desc": status_desc,
                "error": status_error,
            }

        # Origin and Destination details (Biteship style)
        wh = self.picking_type_id.warehouse_id
        origin_city = (wh and wh.partner_id and wh.partner_id.city) or self.company_id.city or "Jakarta"
        origin_info = {
            "name": self.company_id.name or "Pengirim",
            "city": origin_city,
        }

        partner = self.partner_id
        dest_loc = ", ".join(filter(None, [partner.city, partner.state_id.name])) or (partner.country_id and partner.country_id.name) or "Indonesia"
        dest_info = {
            "name": partner.name or "-",
            "city": partner.city or "",
            "location": dest_loc,
        }

        # Multi-carrier segmented tabs info
        courier_tabs = []
        if self.carrier_id or self.carrier_tracking_ref:
            courier_tabs.append({
                "carrier_num": 1,
                "label": "Kurir 1",
                "name": self.carrier_id.name or "Kurir 1",
                "resi": self.carrier_tracking_ref or "",
                "url": self.delivery_tracking_url,
                "is_active": (carrier_num == 1),
            })
        if self.carrier_id_2 or self.carrier_tracking_ref_2:
            courier_tabs.append({
                "carrier_num": 2,
                "label": "Kurir 2",
                "name": self.carrier_id_2.name or "Kurir 2",
                "resi": self.carrier_tracking_ref_2 or "",
                "url": self.delivery_tracking_url_2,
                "is_active": (carrier_num == 2),
            })

        # Build Detailed Progress Events (Biteship Live Journey timeline - reverse chronological)
        carrier_logs = self.tracking_log_ids.filtered(
            lambda l: not l.carrier_id or l.carrier_id == carrier_obj
        ).sorted(key=lambda log: log.event_datetime or dt.min, reverse=True)

        detailed_progress = []
        if carrier_logs:
            for idx, log in enumerate(carrier_logs):
                st_label = self._tracking_status_display_label(log.status).upper()
                detailed_progress.append({
                    "title": st_label,
                    "desc": log.status_desc or "",
                    "location": log.location or "",
                    "date_display": self._format_tracking_date(log.event_datetime),
                    "time_display": self._format_tracking_time(log.event_datetime),
                    "ts_display": self._format_tracking_datetime(log.event_datetime),
                    "is_latest": (idx == 0),
                    "status": log.status,
                    "courier_name": log.carrier_id.name if log.carrier_id else courier_name,
                    "icon": self._get_tracking_event_icon(log.status, log.status_desc or st_label),
                })
            # Add initial warehouse milestones at the base of the history
            if resi:
                detailed_progress.append({
                    "title": "NOMOR RESI DITERBITKAN",
                    "desc": "Nomor resi %s telah diterbitkan. Paket diserahkan ke kurir %s." % (resi, courier_name),
                    "location": origin_city,
                    "date_display": self._format_tracking_date(self.date_done or self.create_date),
                    "time_display": self._format_tracking_time(self.date_done or self.create_date),
                    "ts_display": self._format_tracking_datetime(self.date_done or self.create_date),
                    "is_latest": False,
                    "status": "shipped",
                    "courier_name": courier_name,
                    "icon": "fa-barcode text-info",
                })
            detailed_progress.append({
                "title": "ORDER DIBUAT",
                "desc": "Dokumen pengiriman %s telah dibuat di sistem." % (self.name or ""),
                "location": origin_city,
                "date_display": self._format_tracking_date(self.create_date),
                "time_display": self._format_tracking_time(self.create_date),
                "ts_display": self._format_tracking_datetime(self.create_date),
                "is_latest": False,
                "status": "draft",
                "courier_name": courier_name,
                "icon": "fa-file-text-o text-muted",
            })
        else:
            # Belum ada log dari API kurir
            if resi:
                detailed_progress = [
                    {
                        "title": "RESI TELAH DITERBITKAN",
                        "desc": "Nomor resi %s telah diterbitkan via %s. Menunggu pemindaian perjalanan pertama dari pihak kurir." % (resi, courier_name),
                        "location": origin_city,
                        "date_display": self._format_tracking_date(self.date_done or self.create_date),
                        "time_display": self._format_tracking_time(self.date_done or self.create_date),
                        "ts_display": self._format_tracking_datetime(self.date_done or self.create_date),
                        "is_latest": True,
                        "status": "shipped",
                        "courier_name": courier_name,
                        "icon": "fa-barcode text-info",
                    },
                    {
                        "title": "PESANAN SELESAI DIKEMAS",
                        "desc": "Barang pesanan telah selesai dikemas di gudang dan siap diserahkan ke pihak kurir.",
                        "location": origin_city,
                        "date_display": self._format_tracking_date(self.date_done or self.date or self.create_date),
                        "time_display": self._format_tracking_time(self.date_done or self.date or self.create_date),
                        "ts_display": self._format_tracking_datetime(self.date_done or self.date or self.create_date),
                        "is_latest": False,
                        "status": "in_progress",
                        "courier_name": courier_name,
                        "icon": "fa-archive text-warning",
                    },
                    {
                        "title": "ORDER DIBUAT",
                        "desc": "Dokumen pengiriman %s telah dibuat di sistem." % (self.name or ""),
                        "location": origin_city,
                        "date_display": self._format_tracking_date(self.create_date),
                        "time_display": self._format_tracking_time(self.create_date),
                        "ts_display": self._format_tracking_datetime(self.create_date),
                        "is_latest": False,
                        "status": "draft",
                        "courier_name": courier_name,
                        "icon": "fa-file-text-o text-muted",
                    },
                ]
            else:
                detailed_progress = [
                    {
                        "title": "PESANAN SEDANG DIPROSES",
                        "desc": "Pesanan Anda sedang disiapkan dan dikemas oleh tim gudang kami.",
                        "location": origin_city,
                        "date_display": self._format_tracking_date(self.date or self.create_date),
                        "time_display": self._format_tracking_time(self.date or self.create_date),
                        "ts_display": self._format_tracking_datetime(self.date or self.create_date),
                        "is_latest": True,
                        "status": "draft",
                        "courier_name": courier_name,
                        "icon": "fa-archive text-warning",
                    },
                    {
                        "title": "ORDER DIBUAT",
                        "desc": "Dokumen pengiriman %s telah dibuat di sistem." % (self.name or ""),
                        "location": origin_city,
                        "date_display": self._format_tracking_date(self.create_date),
                        "time_display": self._format_tracking_time(self.create_date),
                        "ts_display": self._format_tracking_datetime(self.create_date),
                        "is_latest": False,
                        "status": "draft",
                        "courier_name": courier_name,
                        "icon": "fa-file-text-o text-muted",
                    },
                ]

        history = [
            {
                "status": log.status,
                "status_label": self._tracking_status_display_label(log.status),
                "desc": log.status_desc,
                "location": log.location,
                "ts_display": self._format_tracking_datetime(log.event_datetime),
                "courier_name": log.carrier_id.name
                if log.carrier_id
                else courier_name,
            }
            for log in self.tracking_log_ids.filtered(
                lambda l: not l.carrier_id or l.carrier_id == carrier_obj
            ).sorted(key=lambda log: log.event_datetime or dt.min)
        ]

        display_label = self._tracking_status_display_label(status)
        if not resi and status == "draft":
            display_label = "Diproses"
            status_desc = status_desc or "Pesanan sedang disiapkan di gudang"

        # Calculate route progress percentage for visual bar
        if status == "delivered":
            route_progress_percent = 100
        elif status == "in_transit":
            route_progress_percent = 70
        elif status == "shipped":
            route_progress_percent = 45
        elif resi:
            route_progress_percent = 25
        else:
            route_progress_percent = 10

        total_weight = self.shipping_weight or self.weight or 0.0
        weight_display = f"{total_weight:.2f} kg" if total_weight > 0 else False

        company_phone = self.company_id.phone or self.company_id.mobile or ""
        company_email = self.company_id.email or ""
        wa_digits = "".join(ch for ch in company_phone if ch.isdigit())
        if wa_digits.startswith("0"):
            wa_digits = "62" + wa_digits[1:]

        # Extract items shipped in this picking
        package_items = []
        try:
            moves = self.move_ids_without_package or self.move_ids
            for move in moves[:12]:
                qty = move.quantity_done if move.quantity_done > 0 else move.product_uom_qty
                if qty > 0:
                    package_items.append({
                        "name": move.product_id.name or move.name or "Item Produk",
                        "qty": ("%g" % qty) if isinstance(qty, (int, float)) else str(qty),
                        "uom": move.product_uom.name if move.product_uom else "Pcs",
                    })
        except Exception:
            package_items = []

        carrier_brand = self._get_carrier_brand_info(carrier_obj)
        order_reference = self.origin or (self.sale_id and self.sale_id.name) or ""

        status_tone_map = {
            "delivered": "success",
            "in_transit": "primary",
            "shipped": "info",
            "draft": "warning",
            "in_progress": "warning",
            "exception": "danger",
            "unknown": "secondary",
        }
        status_tone = status_tone_map.get(status, "primary")

        return {
            "picking": self,
            "picking_name": self.name or "-",
            "order_reference": order_reference,
            "partner_name": self.partner_id.name or "-",
            "resi": resi,
            "courier_name": courier_name,
            "carrier_brand": carrier_brand,
            "eta_display": self._format_tracking_date(eta),
            "last_check_display": self._format_tracking_datetime(last_check),
            "create_date_display": self._format_tracking_datetime(self.create_date),
            "status": status,
            "status_tone": status_tone,
            "current_status": {
                "label": display_label,
                "desc": status_desc,
            },
            "steps": self._get_delivery_tracking_steps(carrier_num=carrier_num),
            "exception": exception,
            "unknown": status == "unknown",
            "tracking_url": tracking_url,
            "history": history,
            "detailed_progress": detailed_progress,
            "origin_info": origin_info,
            "dest_info": dest_info,
            "courier_tabs": courier_tabs,
            "package_items": package_items,
            "route_progress_percent": route_progress_percent,
            "weight_display": weight_display,
            "company_phone": company_phone,
            "company_wa_phone": wa_digits,
            "company_email": company_email,
            "is_public": False,
            "carrier_num": carrier_num,
            "carrier_label": carrier_label,
            "other_carrier": other_carrier,
        }

    def _parse_event_datetime(self, value):
        """Parse an API timestamp into a naive UTC datetime."""
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
        """Best-effort estimated delivery date from the raw Biteship response."""
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

    def _process_tracking_result(self, result, carrier_num=1):
        """Apply a normalized tracking result for Carrier 1 or Carrier 2."""
        self.ensure_one()
        now = fields.Datetime.now()
        status = result.get("status") or "unknown"
        status_desc = result.get("status_desc")
        error_message = result.get("error") if status == "unknown" else False
        estimated_date = self._extract_estimated_date(result)
        raw = result.get("raw_response")
        biteship_id = raw.get("id") if isinstance(raw, dict) else False

        carrier = self.carrier_id if carrier_num == 1 else self.carrier_id_2
        current_retry = (
            self.delivery_tracking_retry_count
            if carrier_num == 1
            else self.delivery_tracking_retry_count_2
        )
        if status == "unknown":
            new_retry = current_retry + 1
            err_str = str(error_message or "").lower()
            is_permanent_error = (
                "400" in err_str
                or "invalid" in err_str
                or "expired" in err_str
                or "not found" in err_str
            )
            if new_retry >= 3 or is_permanent_error:
                status = "exception"
                status_desc = _("Kendala tracking: %s") % (
                    error_message or _("Resi tidak valid atau kadaluarsa")
                )
        else:
            new_retry = 0

        if carrier_num == 1:
            vals = {
                "delivery_last_check": now,
                "delivery_tracking_status": status,
                "delivery_tracking_status_desc": status_desc,
                "delivery_tracking_error": error_message,
                "delivery_tracking_retry_count": new_retry,
            }
            if estimated_date:
                vals["delivery_estimated_date"] = estimated_date
            if biteship_id:
                vals["biteship_tracking_id"] = biteship_id
        else:
            vals = {
                "delivery_last_check_2": now,
                "delivery_tracking_status_2": status,
                "delivery_tracking_status_desc_2": status_desc,
                "delivery_tracking_error_2": error_message,
                "delivery_tracking_retry_count_2": new_retry,
            }
            if estimated_date:
                vals["delivery_estimated_date_2"] = estimated_date
            if biteship_id:
                vals["biteship_tracking_id_2"] = biteship_id

        self.write(vals)

        existing_dates = {
            log.event_datetime
            for log in self.tracking_log_ids
            if log.event_datetime and (not log.carrier_id or log.carrier_id == carrier)
        }
        for item in result.get("history") or []:
            event_datetime = self._parse_event_datetime(item.get("updated_at"))
            if not event_datetime or event_datetime in existing_dates:
                continue
            self.env["delivery.tracking.log"].create(
                {
                    "picking_id": self.id,
                    "carrier_id": carrier.id if carrier else False,
                    "status": TrackingAPIClient._map_status(item.get("status")),
                    "status_desc": item.get("note"),
                    "location": item.get("location"),
                    "event_datetime": event_datetime,
                    "raw_response": json.dumps(item),
                }
            )
            existing_dates.add(event_datetime)

    def _is_dev_environment_with_live_key(self, provider):
        """Safety guard: prevent using live Biteship key on dev/staging/localhost."""
        if not provider or not getattr(provider, "api_key", "").startswith("biteship_live."):
            return False
        icp = self.env["ir.config_parameter"].sudo()
        if icp.get_param("delivery_tracking.allow_live_on_dev", "False").lower() in ("true", "1"):
            return False
        base_url = icp.get_param("web.base.url", "").lower()
        dev_indicators = ["localhost", "127.0.0.1", "test", "staging", ".local"]
        return any(ind in base_url for ind in dev_indicators)

    def _track_single(self, provider=None, force=False):
        """Fetch and process tracking status for Carrier 1 and Carrier 2."""
        self.ensure_one()
        if not provider:
            provider = self._get_active_tracking_provider()
        if not provider or not provider.active or not provider.api_key:
            return False

        if self._is_dev_environment_with_live_key(provider):
            _logger.warning(
                "Delivery tracking: API live Biteship dicegah berjalan di environment non-produksi "
                "(web.base.url: %s). Gunakan sandbox key 'biteship_test...' atau set config parameter "
                "'delivery_tracking.allow_live_on_dev=True' jika memang disengaja.",
                self.env["ir.config_parameter"].sudo().get_param("web.base.url", ""),
            )
            return False

        client = TrackingAPIClient(provider)
        refreshed = False

        # Carrier 1 tracking
        if self.carrier_tracking_ref and self.carrier_id.biteship_courier_code:
            if force or self.delivery_tracking_status not in FINAL_TRACKING_STATUSES:
                res1 = client.get_status(
                    self.carrier_tracking_ref, self.carrier_id.biteship_courier_code
                )
                self._process_tracking_result(res1, carrier_num=1)
                refreshed = True
                if res1.get("error_code") == "insufficient_balance":
                    return "insufficient_balance"
        elif self.carrier_tracking_ref:
            _logger.warning(
                "Delivery tracking: carrier 1 '%s' on %s is not mapped to Biteship.",
                self.carrier_id.name,
                self.name,
            )

        # Carrier 2 tracking
        if self.carrier_tracking_ref_2 and self.carrier_id_2.biteship_courier_code:
            if force or self.delivery_tracking_status_2 not in FINAL_TRACKING_STATUSES:
                res2 = client.get_status(
                    self.carrier_tracking_ref_2, self.carrier_id_2.biteship_courier_code
                )
                self._process_tracking_result(res2, carrier_num=2)
                refreshed = True
                if res2.get("error_code") == "insufficient_balance":
                    return "insufficient_balance"
        elif self.carrier_tracking_ref_2:
            _logger.warning(
                "Delivery tracking: carrier 2 '%s' on %s is not mapped to Biteship.",
                self.carrier_id_2.name,
                self.name,
            )

        return refreshed

    def cron_check_delivery_status(self):
        """Poll tracking status for eligible outgoing done pickings."""
        provider = self._get_active_tracking_provider()
        if not provider or not provider.active or not provider.api_key:
            return False
        if self._is_dev_environment_with_live_key(provider):
            _logger.warning(
                "Delivery tracking cron: live Biteship call blocked on non-production environment."
            )
            return False
        icp = self.env["ir.config_parameter"]
        batch_size = int(
            icp.get_param(
                "delivery_tracking_cron_batch_size",
                default=10,
            )
        )
        domain = [
            ("picking_type_code", "=", "outgoing"),
            ("state", "=", "done"),
            "|",
            "&",
            ("carrier_tracking_ref", "!=", False),
            ("delivery_tracking_status", "not in", FINAL_TRACKING_STATUSES),
            "&",
            ("carrier_tracking_ref_2", "!=", False),
            ("delivery_tracking_status_2", "not in", FINAL_TRACKING_STATUSES),
        ]
        pickings = self.search(domain)
        pickings = pickings.sorted(
            key=lambda p: p.delivery_last_check or dt.min,
            reverse=False,
        )[:batch_size]
        for picking in pickings:
            res = picking._track_single(provider, force=False)
            if res == "insufficient_balance":
                _logger.warning(
                    "Delivery tracking cron aborted: Biteship balance depleted."
                )
                break
        return True

    def action_check_delivery_status(self):
        """Manually refresh tracking status for the current record(s)."""
        for picking in self:
            if (
                picking.carrier_tracking_ref
                and not picking.carrier_id.biteship_courier_code
            ):
                raise UserError(
                    _(
                        "Kurir 1 '%s' belum di-mapping ke Biteship courier code."
                    )
                    % picking.carrier_id.name
                )
            if (
                picking.carrier_tracking_ref_2
                and not picking.carrier_id_2.biteship_courier_code
            ):
                raise UserError(
                    _(
                        "Kurir 2 '%s' belum di-mapping ke Biteship courier code."
                    )
                    % picking.carrier_id_2.name
                )
            if not picking.carrier_tracking_ref and not picking.carrier_tracking_ref_2:
                raise UserError(
                    _("DO %s belum memiliki nomor resi.") % picking.name
                )

        refreshed = 0
        skipped = 0
        for picking in self:
            if picking._track_single(force=True):
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
        """Trigger Telegram notification and reset tracking states on change."""
        changed_1 = "carrier_tracking_ref" in vals or "carrier_id" in vals
        changed_2 = "carrier_tracking_ref_2" in vals or "carrier_id_2" in vals
        changed = changed_1 or changed_2
        previous = {}
        if changed:
            previous = {
                picking.id: (
                    picking.carrier_tracking_ref,
                    picking.carrier_id,
                    picking.carrier_tracking_ref_2,
                    picking.carrier_id_2,
                )
                for picking in self
            }
        res = super().write(vals)
        if changed:
            for picking in self:
                old_ref_1, old_car_1, old_ref_2, old_car_2 = previous.get(
                    picking.id, (None, None, None, None)
                )
                reset_vals = {}
                if (
                    old_ref_1 != picking.carrier_tracking_ref
                    or old_car_1 != picking.carrier_id
                ):
                    reset_vals.update(
                        {
                            "delivery_tracking_status": "draft",
                            "delivery_tracking_status_desc": False,
                            "delivery_tracking_error": False,
                            "delivery_last_check": False,
                            "delivery_tracking_retry_count": 0,
                            "delivery_tracking_token": uuid.uuid4().hex,
                        }
                    )
                if (
                    old_ref_2 != picking.carrier_tracking_ref_2
                    or old_car_2 != picking.carrier_id_2
                ):
                    reset_vals.update(
                        {
                            "delivery_tracking_status_2": "draft",
                            "delivery_tracking_status_desc_2": False,
                            "delivery_tracking_error_2": False,
                            "delivery_last_check_2": False,
                            "delivery_tracking_retry_count_2": 0,
                            "delivery_tracking_token_2": uuid.uuid4().hex,
                        }
                    )
                if reset_vals:
                    reset_vals["tracking_notification_sent"] = False
                    picking.write(reset_vals)

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
                    has_resi = bool(
                        picking.carrier_tracking_ref
                        or picking.carrier_tracking_ref_2
                    )
                    if has_resi and not picking.tracking_notification_sent:
                        try:
                            picking._notify_sales_telegram()
                        except Exception:
                            _logger.exception(
                                "Delivery tracking Telegram notification failed for %s",
                                picking.name,
                            )
        return res

    def _notify_sales_telegram(self):
        """Send the tracking-reference notification to the DO's salesperson."""
        self.ensure_one()
        salesperson = self.sale_id.user_id if self.sale_id else False
        if not salesperson or not salesperson.telegram_chat_id:
            _logger.info(
                "DO %s: salesperson has no telegram_chat_id, notification skipped",
                self.name,
            )
            return

        try:
            self._track_single()
        except Exception as exc:
            _logger.warning(
                "Delivery tracking: Biteship refresh failed for %s before "
                "Telegram notification, using existing data: %s",
                self.name,
                exc,
            )
        self.invalidate_recordset()
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

    def _get_tracking_url(self, carrier_num=1):
        """Public tracking URL for the picking, best available source first."""
        self.ensure_one()
        if carrier_num == 2:
            if self.biteship_tracking_id_2:
                return "https://track.biteship.com/%s" % self.biteship_tracking_id_2
            return False
        if self.biteship_tracking_id:
            return "https://track.biteship.com/%s" % self.biteship_tracking_id
        return self.carrier_tracking_url or False

    def _build_tracking_notification_message(self):
        self.ensure_one()
        url_1 = (
            self.delivery_tracking_url
            or self._get_tracking_url(carrier_num=1)
            or ""
        )
        url_2 = (
            self.delivery_tracking_url_2
            or self._get_tracking_url(carrier_num=2)
            or ""
        )
        if self.carrier_tracking_ref_2:
            return _(
                "*DO %(name)s* sudah diinput resi\n"
                "Customer: %(customer)s\n"
                "Kurir 1: %(courier_1)s\n"
                "No. Resi 1: %(resi_1)s\n"
                "Link Tracking 1: %(url_1)s\n"
                "Kurir 2: %(courier_2)s\n"
                "No. Resi 2: %(resi_2)s\n"
                "Link Tracking 2: %(url_2)s"
            ) % {
                "name": self.name or "-",
                "customer": self.partner_id.name or "-",
                "courier_1": self.carrier_id.name or "-",
                "resi_1": self.carrier_tracking_ref or "-",
                "url_1": url_1 or "(belum tersedia)",
                "courier_2": self.carrier_id_2.name or "-",
                "resi_2": self.carrier_tracking_ref_2 or "-",
                "url_2": url_2 or "(belum tersedia)",
            }
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
            "url": url_1 or "(belum tersedia)",
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
        """Manually resend the Telegram notification to the salesperson."""
        for picking in self:
            if not picking.carrier_tracking_ref and not picking.carrier_tracking_ref_2:
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
        """Bulk refresh tracking status for selected Delivery Orders."""
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
                if picking._track_single(force=True):
                    refreshed += 1
                else:
                    failed += 1
                    if not picking.carrier_tracking_ref and not picking.carrier_tracking_ref_2:
                        reason = _("%s: nomor resi kosong") % picking.name
                    else:
                        reason = _("%s: kurir belum di-mapping atau tidak ada provider aktif") % picking.name
                    failed_reasons.append(reason)
            except Exception as exc:
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
