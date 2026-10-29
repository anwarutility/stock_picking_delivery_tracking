# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

from odoo import http, _
from odoo.exceptions import AccessError, MissingError
from odoo.http import request

from odoo.addons.portal.controllers.portal import CustomerPortal


class DeliveryTrackingPortal(CustomerPortal):
    """Customer-facing delivery tracking pages.

    Two entry points:
    - ``/my/orders/<order_id>/tracking`` — logged-in portal users, access is
      checked against the partner owning the sale order.
    - ``/tracking/<token>`` — public, no login. The token is a 128-bit random
      secret stored per carrier on the picking, so the page can never be guessed
      and does not expose anything beyond tracking info (no prices, no other customers).
    """

    def _get_order_picking(self, order_id):
        """First outgoing picking (with a resi 1 or resi 2) linked to the sale order."""
        return request.env["stock.picking"].sudo().search(
            [
                ("sale_id", "=", order_id),
                ("picking_type_code", "=", "outgoing"),
                "|",
                ("carrier_tracking_ref", "!=", False),
                ("carrier_tracking_ref_2", "!=", False),
            ],
            limit=1,
        )

    def _prepare_tracking_page_values(self, picking_sudo, is_public=False, carrier_num=1):
        values = picking_sudo._get_delivery_tracking_data(carrier_num=carrier_num)
        values["is_public"] = is_public
        values.update(self._prepare_portal_layout_values())
        values["page_name"] = "tracking"
        return values

    @http.route(
        ["/my/orders/<int:order_id>/tracking"],
        type="http",
        auth="user",
        website=True,
    )
    def portal_my_order_tracking(self, order_id, carrier="1", **kw):
        """Tracking page for a logged-in portal user (own order only)."""
        try:
            order_sudo = self._document_check_access("sale.order", order_id)
        except (AccessError, MissingError):
            return request.redirect("/my")
        picking_sudo = self._get_order_picking(order_id)
        if not picking_sudo:
            return request.not_found()
        carrier_num = 2 if str(carrier) == "2" else 1
        values = self._prepare_tracking_page_values(picking_sudo, carrier_num=carrier_num)
        return request.render(
            "stock_picking_delivery_tracking.portal_tracking_page", values
        )

    @http.route(
        ["/tracking/<string:token>"],
        type="http",
        auth="public",
        website=True,
    )
    def public_delivery_tracking(self, token, **kw):
        """Public no-login tracking page, keyed by the per-carrier secret token."""
        picking_sudo = request.env["stock.picking"].sudo().search(
            [
                "|",
                ("delivery_tracking_token", "=", token),
                ("delivery_tracking_token_2", "=", token),
            ],
            limit=1,
        )
        if not picking_sudo:
            return request.not_found()
        carrier_num = 2 if picking_sudo.delivery_tracking_token_2 == token else 1
        values = self._prepare_tracking_page_values(
            picking_sudo, is_public=True, carrier_num=carrier_num
        )
        return request.render(
            "stock_picking_delivery_tracking.portal_tracking_page", values
        )
