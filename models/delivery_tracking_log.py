# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

from odoo import fields, models


class DeliveryTrackingLog(models.Model):
    _name = "delivery.tracking.log"
    _description = "Delivery Tracking Log"
    _order = "event_datetime desc, id desc"

    picking_id = fields.Many2one(
        "stock.picking",
        string="Delivery Order",
        required=True,
        ondelete="cascade",
    )
    status = fields.Selection(
        [
            ("draft", "Draft"),
            ("shipped", "Shipped"),
            ("in_transit", "In Transit"),
            ("delivered", "Delivered"),
            ("exception", "Exception"),
            ("unknown", "Unknown"),
        ],
        string="Status",
        required=True,
    )
    status_desc = fields.Char(string="Status Description")
    location = fields.Char(string="Location")
    event_datetime = fields.Datetime(string="Event Datetime")
    raw_response = fields.Text(string="Raw Response")
