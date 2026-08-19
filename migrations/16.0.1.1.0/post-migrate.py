"""Post-migration for 16.0.1.1.0.

Existing Delivery Orders that already have a carrier_tracking_ref are marked
as ``tracking_notification_sent = TRUE`` so upgrading does not fire a
retroactive Telegram notification flood. Only newly entered resi after the
upgrade trigger the one-time salesperson notification.
"""


def migrate(cr, version):
    cr.execute(
        """
        UPDATE stock_picking
        SET tracking_notification_sent = TRUE
        WHERE carrier_tracking_ref IS NOT NULL
          AND carrier_tracking_ref != ''
        """
    )
