# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

from odoo import api, fields, models


class DeliveryCarrier(models.Model):
    _inherit = "delivery.carrier"

    biteship_courier_code = fields.Char(
        string="Biteship Courier Code",
        help="Courier code used by the Biteship API. Confirmed values: "
        "jne=JNE, jnt=J&T, jntcargo=J&T Cargo, lion=Lion, wahana=Wahana, pos=Pos Indonesia.",
    )

    @api.model
    def _ensure_jnt_cargo_carrier(self):
        """Ensure J&T Cargo carrier exists and is mapped to biteship_courier_code='jntcargo'."""
        carrier = self.search([("biteship_courier_code", "=", "jntcargo")], limit=1)
        if not carrier:
            carrier = self.search([("name", "ilike", "J&T Cargo")], limit=1)
        if not carrier:
            product = self.env.ref(
                "stock_picking_delivery_tracking.product_product_delivery_jntcargo",
                raise_if_not_found=False,
            )
            if not product:
                product = self.env.ref(
                    "delivery.product_product_delivery",
                    raise_if_not_found=False,
                )
            if not product:
                product = self.env["product.product"].search(
                    [("type", "=", "service")], limit=1
                )
            if not product:
                product = self.env["product.product"].create(
                    {
                        "name": "J&T Cargo Delivery",
                        "type": "service",
                        "list_price": 0.0,
                    }
                )
            carrier = self.create(
                {
                    "name": "J&T Cargo",
                    "sequence": 15,
                    "delivery_type": "fixed",
                    "biteship_courier_code": "jntcargo",
                    "product_id": product.id,
                    "fixed_price": 0.0,
                }
            )
        else:
            vals = {}
            if not carrier.biteship_courier_code:
                vals["biteship_courier_code"] = "jntcargo"
            if not carrier.active:
                vals["active"] = True
            if vals:
                carrier.write(vals)
        return carrier

    def init(self):
        super().init()
        # Ensure default J&T Cargo carrier exists upon module load/upgrade
        try:
            self._ensure_jnt_cargo_carrier()
        except Exception:
            # Table or models may still be initializing in some phase
            pass
