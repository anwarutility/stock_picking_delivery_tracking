# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    delivery_tracking_cron_batch_size = fields.Integer(
        string="Cron Batch Size",
        default=50,
        config_parameter="delivery_tracking_cron_batch_size",
        help="Maksimal jumlah DO yang dicek statusnya dalam sekali jalan cron.",
    )
    delivery_tracking_auto_track_enabled = fields.Boolean(
        string="Auto Track via Biteship",
        default=True,
        help="Aktifkan polling status pengiriman ke Biteship.",
    )
    delivery_tracking_telegram_bot_token = fields.Char(
        string="Telegram Bot Token",
        default="",
        config_parameter="stock_picking_delivery_tracking.telegram_bot_token",
        help="Token bot Telegram (dari @BotFather). Jangan bagikan token ini.",
    )
    delivery_tracking_notify_sales_enabled = fields.Boolean(
        string="Notify Salesperson on Tracking Reference",
        default=True,
        config_parameter="stock_picking_delivery_tracking.notify_sales_enabled",
        help="Kirim notifikasi Telegram ke salesperson saat nomor resi "
        "pertama kali diinput pada Delivery Order.",
    )

    @api.model
    def get_values(self):
        res = super().get_values()
        cron = self.env.ref(
            "stock_picking_delivery_tracking.ir_cron_delivery_tracking_poll",
            raise_if_not_found=False,
        )
        res["delivery_tracking_auto_track_enabled"] = (
            cron.active if cron else False
        )
        return res

    def set_values(self):
        super().set_values()
        cron = self.env.ref(
            "stock_picking_delivery_tracking.ir_cron_delivery_tracking_poll",
            raise_if_not_found=False,
        )
        if cron and cron.active != self.delivery_tracking_auto_track_enabled:
            cron.sudo().write(
                {"active": self.delivery_tracking_auto_track_enabled}
            )
