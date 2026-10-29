"""Post-migration for 16.0.1.6.0.

Backfill ``delivery_tracking_token_2`` for existing Delivery Orders.
"""


def migrate(cr, version):
    cr.execute(
        """
        UPDATE stock_picking
        SET delivery_tracking_token_2 = substr(
            replace(gen_random_uuid()::text, '-', ''),
            1, 32
        )
        WHERE delivery_tracking_token_2 IS NULL
           OR delivery_tracking_token_2 = ''
        """
    )
