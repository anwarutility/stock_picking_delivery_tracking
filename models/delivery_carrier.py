# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

from odoo import fields, models


class DeliveryCarrier(models.Model):
    _inherit = "delivery.carrier"

    biteship_courier_code = fields.Char(
        string="Biteship Courier Code",
        help="Courier code used by the Biteship API. Confirmed values: "
        "jne=JNE, jnt=J&T, lion=Lion, wahana=Wahana, pos=Pos Indonesia.",
    )
