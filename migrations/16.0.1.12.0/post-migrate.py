"""Post-migration for 16.0.1.12.0.

Ensure existing Delivery Orders have distinct public tracking tokens 1 and 2
and ensure the canonical J&T Cargo carrier record exists.
"""
from odoo import SUPERUSER_ID, api
from odoo.addons.stock_picking_delivery_tracking.hooks import (
    ensure_unique_tracking_tokens,
)


def migrate(cr, version):
    ensure_unique_tracking_tokens(cr)
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["delivery.carrier"]._ensure_jnt_cargo_carrier()
