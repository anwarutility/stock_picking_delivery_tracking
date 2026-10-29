"""Post-migration for 16.0.1.6.0.

Backfill ``stock.picking.tracking.line`` for existing Delivery Orders
that already have carrier_id and carrier_tracking_ref set on stock_picking.
"""


def migrate(cr, version):
    cr.execute(
        """
        INSERT INTO stock_picking_tracking_line (
            picking_id,
            sequence,
            carrier_id,
            carrier_tracking_ref,
            delivery_tracking_status,
            delivery_tracking_status_desc,
            delivery_last_check,
            delivery_estimated_date,
            delivery_tracking_error,
            biteship_tracking_id,
            create_uid,
            create_date,
            write_uid,
            write_date
        )
        SELECT
            sp.id,
            10,
            sp.carrier_id,
            sp.carrier_tracking_ref,
            COALESCE(sp.delivery_tracking_status, 'draft'),
            sp.delivery_tracking_status_desc,
            sp.delivery_last_check,
            sp.delivery_estimated_date,
            sp.delivery_tracking_error,
            sp.biteship_tracking_id,
            COALESCE(sp.create_uid, 1),
            NOW(),
            COALESCE(sp.write_uid, 1),
            NOW()
        FROM stock_picking sp
        WHERE sp.carrier_id IS NOT NULL
          AND sp.carrier_tracking_ref IS NOT NULL
          AND sp.carrier_tracking_ref != ''
          AND NOT EXISTS (
              SELECT 1 FROM stock_picking_tracking_line sptl
              WHERE sptl.picking_id = sp.id
          )
        """
    )
