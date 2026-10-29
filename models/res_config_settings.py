# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    delivery_tracking_cron_interval_minutes = fields.Integer(
        string="Delivery Tracking Poll Interval (minutes)",
        default=30,
        config_parameter="delivery_tracking_cron_interval_minutes",
    )
    delivery_tracking_cron_batch_size = fields.Integer(
        string="Delivery Tracking Batch Size per Run",
        default=50,
        config_parameter="delivery_tracking_cron_batch_size",
    )
    biteship_api_key = fields.Char(
        string="Biteship API Key",
        default="",
        help="API key dari dashboard.biteship.com/integrations. "
        "Prefix biteship_live. (production) atau biteship_test. (sandbox).",
    )
    biteship_active = fields.Boolean(
        string="Aktifkan Tracking",
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
        provider = self._get_biteship_provider()
        res.update(
            {
                "biteship_api_key": provider.api_key if provider else "",
                "biteship_active": provider.active if provider else True,
            }
        )
        return res

    def set_values(self):
        super().set_values()
        provider = self._get_biteship_provider(create=True)
        provider.write(
            {
                "api_key": self.biteship_api_key or "",
                "active": self.biteship_active,
            }
        )
        if self.delivery_tracking_cron_interval_minutes:
            cron = self.env.ref(
                "stock_picking_delivery_tracking.cron_delivery_tracking_status_poll",
                raise_if_not_found=False,
            )
            if cron:
                cron.write(
                    {
                        "interval_number": self.delivery_tracking_cron_interval_minutes,
                        "interval_type": "minutes",
                    }
                )

    def _get_biteship_provider(self, create=False):
        providers = self.env["delivery.tracking.provider"].search(
            [("name", "=", "Biteship")]
        )
        if providers:
            return providers[0]
        if create:
            return self.env["delivery.tracking.provider"].create(
                {
                    "name": "Biteship",
                    "api_base_url": "https://api.biteship.com",
                    "api_key": "",
                    "auth_type": "api_key_header",
                    "auth_header_name": "Authorization",
                    "active": True,
                }
            )
        return self.env["delivery.tracking.provider"]
