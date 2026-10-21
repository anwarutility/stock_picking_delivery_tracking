"""Post-migration for 16.0.1.5.0.

Backfill ``delivery_tracking_token`` for Delivery Orders created before the
public tracking portal existed, so their public no-login links work right away
after the upgrade. New records get the token automatically on create and it is
regenerated whenever the carrier / tracking reference changes.
"""


def migrate(cr, version):
    cr.execute(
        """
        UPDATE stock_picking
        SET delivery_tracking_token = substr(
            replace(gen_random_uuid()::text, '-', ''),
            1, 32
        )
        WHERE delivery_tracking_token IS NULL
           OR delivery_tracking_token = ''
        """
    )