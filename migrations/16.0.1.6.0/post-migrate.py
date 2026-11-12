"""Post-migration for 16.0.1.6.0.

Backfill and deduplicate public tracking tokens for existing Delivery Orders.
"""
from odoo.addons.stock_picking_delivery_tracking.hooks import (
    ensure_unique_tracking_tokens,
)


def migrate(cr, version):
    ensure_unique_tracking_tokens(cr)
