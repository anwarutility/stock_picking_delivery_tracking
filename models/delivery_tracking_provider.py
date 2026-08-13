# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

from odoo import fields, models


class DeliveryTrackingProvider(models.Model):
    _name = "delivery.tracking.provider"
    _description = "Delivery Tracking Provider"

    name = fields.Char(string="Name", required=True)
    api_base_url = fields.Char(string="API Base URL", required=True)
    api_key = fields.Char(
        string="API Key",
        groups="stock.group_stock_manager,base.group_system",
    )
    auth_type = fields.Selection(
        [
            ("api_key_header", "Raw API Key Header"),
            ("bearer_token", "Bearer Token"),
            ("basic_auth", "Basic Auth"),
        ],
        string="Auth Type",
        default="api_key_header",
        required=True,
    )
    auth_header_name = fields.Char(
        string="Auth Header Name",
        default="Authorization",
    )
    active = fields.Boolean(string="Active", default=True)
