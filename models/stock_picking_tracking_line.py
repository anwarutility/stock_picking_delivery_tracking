# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

import json
import logging
from odoo import _, api, fields, models
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


class StockPickingTrackingLine(models.Model):
    _name = "stock.picking.tracking.line"
    _description = "Delivery Order Tracking Line"
    _order = "sequence, id"

    picking_id = fields.Many2one(
        "stock.picking",
        string="Delivery Order",
        required=True,
        ondelete="cascade",
        index=True,
    )
    sequence = fields.Integer(string="Sequence", default=10)
    carrier_id = fields.Many2one(
        "delivery.carrier",
        string="Kurir",
        required=True,
    )
    carrier_tracking_ref = fields.Char(
        string="No. Resi",
        required=True,
    )
    delivery_tracking_status = fields.Selection(
        TRACKING_STATUS_SELECTION,
        string="Status",
        default="draft",
    )
    delivery_tracking_status_desc = fields.Char(
        string="Keterangan",
    )
    delivery_last_check = fields.Datetime(
        string="Terakhir Dicek",
    )
    delivery_estimated_date = fields.Datetime(
        string="Estimasi Tiba",
    )
    delivery_tracking_error = fields.Char(
        string="Error Tracking",
        help="Pesan error dari provider saat tracking terakhir gagal.",
    )
    biteship_tracking_id = fields.Char(
        string="Biteship Tracking ID",
        copy=False,
    )
    tracking_url = fields.Char(
        string="Link Tracking",
        compute="_compute_tracking_url",
    )

    @api.depends("carrier_id", "carrier_tracking_ref", "biteship_tracking_id")
    def _compute_tracking_url(self):
        for line in self:
            if line.biteship_tracking_id:
                line.tracking_url = "https://track.biteship.com/%s" % line.biteship_tracking_id
            elif (
                line.carrier_id
                and getattr(line.carrier_id, "tracking_url", False)
                and line.carrier_tracking_ref
            ):
                try:
                    line.tracking_url = line.carrier_id.tracking_url.replace(
                        "{tracking_ref}", line.carrier_tracking_ref
                    )
                except Exception:
                    line.tracking_url = False
            else:
                line.tracking_url = False

    def _process_tracking_result(self, result):
        self.ensure_one()
        now = fields.Datetime.now()
        vals = {
            "delivery_last_check": now,
            "delivery_tracking_status": result.get("status") or "unknown",
            "delivery_tracking_status_desc": result.get("status_desc"),
        }
        error_message = result.get("error")
        if error_message and result.get("status") == "unknown":
            vals["delivery_tracking_error"] = error_message
        else:
            vals["delivery_tracking_error"] = False

        estimated_date = self.picking_id._extract_estimated_date(result)
        if estimated_date:
            vals["delivery_estimated_date"] = estimated_date

        raw = result.get("raw_response")
        if isinstance(raw, dict) and raw.get("id"):
            vals["biteship_tracking_id"] = raw.get("id")

        self.write(vals)

        # Create tracking logs for this line
        existing_dates = {
            log.event_datetime
            for log in self.picking_id.tracking_log_ids
            if log.event_datetime and (log.tracking_line_id == self or not log.tracking_line_id)
        }
        for item in result.get("history") or []:
            event_datetime = self.picking_id._parse_event_datetime(item.get("updated_at"))
            if not event_datetime or event_datetime in existing_dates:
                continue
            self.env["delivery.tracking.log"].create({
                "picking_id": self.picking_id.id,
                "tracking_line_id": self.id,
                "carrier_id": self.carrier_id.id,
                "status": TrackingAPIClient._map_status(item.get("status")),
                "status_desc": item.get("note"),
                "location": item.get("location"),
                "event_datetime": event_datetime,
                "raw_response": json.dumps(item),
            })
            existing_dates.add(event_datetime)

    def _track_single(self, provider=None):
        self.ensure_one()
        if not self.carrier_tracking_ref:
            _logger.warning("Delivery tracking line %s has no tracking ref.", self.id)
            return False
        if not self.carrier_id.biteship_courier_code:
            _logger.warning(
                "Delivery tracking: carrier '%s' on line %s is not mapped to Biteship.",
                self.carrier_id.name,
                self.id,
            )
            return False
        if not provider:
            provider = self.picking_id._get_active_tracking_provider()
        if not provider:
            return False
        client = TrackingAPIClient(provider)
        result = client.get_status(
            self.carrier_tracking_ref, self.carrier_id.biteship_courier_code
        )
        self._process_tracking_result(result)
        return True
