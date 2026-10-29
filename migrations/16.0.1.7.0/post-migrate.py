"""Post-migration for 16.0.1.7.0.

Ensure J&T Cargo carrier exists in delivery.carrier with biteship_courier_code = 'jntcargo'.
"""
import logging
from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    try:
        env["delivery.carrier"]._ensure_jnt_cargo_carrier()
        _logger.info("Post-migrate 16.0.1.7.0: J&T Cargo carrier ensured.")
    except Exception as exc:
        _logger.warning("Post-migrate 16.0.1.7.0 error ensuring J&T Cargo: %s", exc)
