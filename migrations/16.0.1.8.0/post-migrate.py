"""Post-migration for 16.0.1.8.0.

Cleanup duplicate J&T Cargo carrier records so only 1 canonical record remains.
"""
import logging
from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    try:
        Carrier = env["delivery.carrier"].with_context(active_test=False)
        canonical = env.ref(
            "stock_picking_delivery_tracking.delivery_carrier_jntcargo",
            raise_if_not_found=False,
        )
        all_jnt = Carrier.search(
            [
                "|",
                ("name", "ilike", "J&T Cargo"),
                ("biteship_courier_code", "=", "jntcargo"),
            ],
            order="id asc",
        )
        if not canonical and all_jnt:
            canonical = all_jnt[0]

        if canonical and len(all_jnt) > 1:
            duplicates = all_jnt.filtered(lambda c: c.id != canonical.id)
            for dup in duplicates:
                env["stock.picking"].search([("carrier_id", "=", dup.id)]).write(
                    {"carrier_id": canonical.id}
                )
                env["stock.picking"].search([("carrier_id_2", "=", dup.id)]).write(
                    {"carrier_id_2": canonical.id}
                )
                env["delivery.tracking.log"].search(
                    [("carrier_id", "=", dup.id)]
                ).write({"carrier_id": canonical.id})
                if "sale.order" in env:
                    env["sale.order"].search([("carrier_id", "=", dup.id)]).write(
                        {"carrier_id": canonical.id}
                    )
                try:
                    dup.unlink()
                    _logger.info(
                        "Post-migrate 16.0.1.8.0: Deleted duplicate J&T Cargo carrier ID %s",
                        dup.id,
                    )
                except Exception as exc:
                    dup.write({"active": False, "name": dup.name + " (Archived Duplicate)"})
                    _logger.warning(
                        "Post-migrate 16.0.1.8.0: Archived duplicate J&T Cargo carrier ID %s: %s",
                        dup.id,
                        exc,
                    )

        if canonical:
            canonical.write(
                {
                    "name": "J&T Cargo",
                    "biteship_courier_code": "jntcargo",
                    "active": True,
                }
            )
            _logger.info(
                "Post-migrate 16.0.1.8.0: Canonical J&T Cargo carrier ID %s ensured.",
                canonical.id,
            )
    except Exception as exc:
        _logger.error("Post-migrate 16.0.1.8.0 error during cleanup: %s", exc)
