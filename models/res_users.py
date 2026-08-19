# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

from odoo import fields, models


class ResUsers(models.Model):
    _inherit = "res.users"

    telegram_chat_id = fields.Char(
        string="Telegram Chat ID",
        help="Chat ID for Telegram notifications. Leave empty if not registered.",
    )
