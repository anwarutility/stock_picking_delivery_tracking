# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0).
"""Installation hooks and shared database maintenance helpers."""
import logging

_logger = logging.getLogger(__name__)


def ensure_unique_tracking_tokens(cr):
    """Ensure every stock.picking has unique, non-empty public tracking tokens.

    Odoo evaluates Python ``default`` lambdas once per column when adding a
    new column across existing rows. This preserves the earliest picking
    (smallest ``id``) for each distinct token and regenerates NULL, empty,
    or duplicate values for both Carrier 1 and Carrier 2 tokens.
    """
    for col in ("delivery_tracking_token", "delivery_tracking_token_2"):
        cr.execute(
            f"""
            UPDATE stock_picking
            SET {col} = substr(
                replace(gen_random_uuid()::text, '-', ''),
                1, 32
            )
            WHERE {col} IS NULL
               OR {col} = ''
               OR id NOT IN (
                   SELECT MIN(id)
                   FROM stock_picking
                   WHERE {col} IS NOT NULL
                     AND {col} <> ''
                   GROUP BY {col}
               )
            """
        )
        if cr.rowcount:
            _logger.info(
                "Delivery tracking: regenerated %s missing/duplicate %s values.",
                cr.rowcount,
                col,
            )


def post_init_hook(cr, registry):
    """Deduplicate tracking tokens immediately after fresh installation."""
    ensure_unique_tracking_tokens(cr)
