"""Post-migration for 16.0.1.13.0.

1. Lock cron interval to 1440 minutes and batch size to 10.
2. Fix historical stuck Delivery Orders that failed with errors into 'exception'.
"""
import logging
from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # 1. Update ir.cron to 1440 minutes (24 hours)
    cron = env.ref(
        "stock_picking_delivery_tracking.cron_delivery_tracking_status_poll",
        raise_if_not_found=False,
    )
    if cron:
        cron.sudo().write({
            "interval_number": 1440,
            "interval_type": "minutes",
        })
        _logger.info("Delivery tracking: ir.cron interval set to 1440 minutes.")

    # 2. Update config parameters
    icp = env["ir.config_parameter"].sudo()
    icp.set_param("delivery_tracking_cron_interval_minutes", 1440)
    icp.set_param("delivery_tracking_cron_batch_size", 10)

    # 3. Cleanup existing stuck records where status='unknown' and error is present
    cr.execute("""
        UPDATE stock_picking
        SET delivery_tracking_status = 'exception',
            delivery_tracking_status_desc = COALESCE(delivery_tracking_status_desc, 'Kendala tracking: ' || COALESCE(delivery_tracking_error, 'Resi tidak valid / kadaluarsa'))
        WHERE delivery_tracking_status = 'unknown'
          AND delivery_tracking_error IS NOT NULL;
    """)
    updated_1 = cr.rowcount

    cr.execute("""
        UPDATE stock_picking
        SET delivery_tracking_status_2 = 'exception',
            delivery_tracking_status_desc_2 = COALESCE(delivery_tracking_status_desc_2, 'Kendala tracking: ' || COALESCE(delivery_tracking_error_2, 'Resi tidak valid / kadaluarsa'))
        WHERE delivery_tracking_status_2 = 'unknown'
          AND delivery_tracking_error_2 IS NOT NULL;
    """)
    updated_2 = cr.rowcount
    _logger.info(
        "Delivery tracking: cleaned up %d stuck carrier 1 and %d stuck carrier 2 pickings to 'exception'.",
        updated_1,
        updated_2,
    )
