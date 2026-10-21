# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

from unittest.mock import patch

from odoo.tests.common import TransactionCase

SEND_MESSAGE = (
    "odoo.addons.stock_picking_delivery_tracking.models."
    "stock_picking.StockPicking._send_telegram_message"
)
PARAM_PREFIX = "stock_picking_delivery_tracking"


class TestTrackingTelegramNotification(TransactionCase):
    """One-time Telegram notification to the DO salesperson on resi input.

    Every ``carrier_tracking_ref`` write is triggered via the model's
    ``write()``; ``_send_telegram_message`` is always mocked so no real HTTP
    call ever reaches api.telegram.org.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Picking = cls.env["stock.picking"]
        cls.Icp = cls.env["ir.config_parameter"]

        cls.partner = cls.env["res.partner"].create(
            {"name": "Customer Telegram"}
        )
        cls.pricelist = cls.env["product.pricelist"].create(
            {"name": "Test Pricelist"}
        )
        cls.salesperson = cls.env["res.users"].create(
            {
                "name": "Salesperson Telegram",
                "login": "sales_telegram",
                "telegram_chat_id": "987654321",
            }
        )
        cls.carrier = cls.env["delivery.carrier"].create(
            {
                "name": "JNE",
                "delivery_type": "fixed",
                "product_id": cls.env["product.product"]
                .create(
                    {
                        "name": "JNE Delivery",
                        "type": "service",
                        "list_price": 0,
                    }
                )
                .id,
            }
        )

        cls.Icp.sudo().set_param(
            "%s.notify_sales_enabled" % PARAM_PREFIX, "True"
        )
        cls.Icp.sudo().set_param(
            "%s.telegram_bot_token" % PARAM_PREFIX, "TEST:TOKEN"
        )

    def _make_sale_picking(self):
        product = self.env["product.product"].create(
            {"name": "Product Telegram", "type": "product"}
        )
        so = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "user_id": self.salesperson.id,
                "pricelist_id": self.pricelist.id,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": product.id,
                            "product_uom_qty": 1,
                        },
                    )
                ],
            }
        )
        so.action_confirm()
        picking = so.picking_ids
        self.assertTrue(picking, "SO confirmation should create a picking")
        picking.carrier_id = self.carrier
        return picking

    # ---- Test 1: sent once on first resi input with correct chat/message ----
    def test_01_notification_sent_on_first_resi(self):
        picking = self._make_sale_picking()
        with patch(SEND_MESSAGE) as mock_send:
            picking.write({"carrier_tracking_ref": "JNE-ABC-1"})

        mock_send.assert_called_once()
        chat_id, message = mock_send.call_args[0]
        self.assertEqual(chat_id, "987654321")
        self.assertIn(picking.name, message)
        self.assertIn("JNE-ABC-1", message)
        picking.invalidate_recordset()
        self.assertTrue(picking.tracking_notification_sent)

    # ---- Test 2: never sent twice when resi is edited again ----
    def test_02_no_double_send_on_resi_edit(self):
        picking = self._make_sale_picking()
        with patch(SEND_MESSAGE) as mock_send:
            picking.write({"carrier_tracking_ref": "JNE-ABC-1"})
            picking.write({"carrier_tracking_ref": "JNE-ABC-1-CORRECTED"})

        mock_send.assert_called_once()
        picking.invalidate_recordset()
        self.assertTrue(picking.tracking_notification_sent)

    # ---- Test 3: missing chat_id skips gracefully without error ----
    def test_03_no_chat_id_skips_without_error(self):
        self.salesperson.telegram_chat_id = False
        picking = self._make_sale_picking()
        with patch(SEND_MESSAGE) as mock_send:
            picking.write({"carrier_tracking_ref": "JNE-ABC-3"})

        mock_send.assert_not_called()
        picking.invalidate_recordset()
        self.assertEqual(picking.carrier_tracking_ref, "JNE-ABC-3")
        self.assertFalse(picking.tracking_notification_sent)

    # ---- Test 4: send failure does not block write, flag stays False ----
    def test_04_send_failure_does_not_block_write(self):
        picking = self._make_sale_picking()
        with patch(
            SEND_MESSAGE, side_effect=Exception("network error")
        ) as mock_send:
            picking.write({"carrier_tracking_ref": "JNE-ABC-4"})
            # picking is still writable afterwards (not left in a bad state)
            picking.write({"name": picking.name})

        mock_send.assert_called_once()
        picking.invalidate_recordset()
        self.assertEqual(picking.carrier_tracking_ref, "JNE-ABC-4")
        self.assertFalse(picking.tracking_notification_sent)

    # ---- Test 5: toggle notify_sales_enabled=False -> never sent ----
    def test_05_notify_disabled_no_notification(self):
        self.Icp.sudo().set_param(
            "%s.notify_sales_enabled" % PARAM_PREFIX, "False"
        )
        picking = self._make_sale_picking()
        with patch(SEND_MESSAGE) as mock_send:
            picking.write({"carrier_tracking_ref": "JNE-ABC-5"})

        mock_send.assert_not_called()
        picking.invalidate_recordset()
        self.assertFalse(picking.tracking_notification_sent)

    # ---- Test 6: message format contains DO/customer/courier/resi/link ----
    def test_06_message_format(self):
        picking = self._make_sale_picking()
        with patch(SEND_MESSAGE):
            picking.write({"carrier_tracking_ref": "JNE-ABC-6"})

        with patch(
            "odoo.addons.delivery.models.delivery_carrier."
            "DeliveryCarrier.get_tracking_link",
            return_value="https://track.example.com/JNE-ABC-6",
        ):
            picking.invalidate_recordset()
            message = picking._build_tracking_notification_message()

        self.assertIn(picking.name, message)
        self.assertIn(self.partner.name, message)
        self.assertIn(self.carrier.name, message)
        self.assertIn("JNE-ABC-6", message)
        self.assertIn("https://track.example.com/JNE-ABC-6", message)

    # ---- Test 7: manual resend button retries a previously failed send ----
    def test_07_manual_resend_button(self):
        picking = self._make_sale_picking()
        with patch(
            SEND_MESSAGE, side_effect=Exception("network error")
        ):
            picking.write({"carrier_tracking_ref": "JNE-ABC-7"})
        picking.invalidate_recordset()
        self.assertFalse(picking.tracking_notification_sent)

        with patch(SEND_MESSAGE) as mock_send:
            action = picking.action_resend_tracking_notification()

        mock_send.assert_called_once()
        chat_id, message = mock_send.call_args[0]
        self.assertEqual(chat_id, "987654321")
        self.assertIn("JNE-ABC-7", message)
        picking.invalidate_recordset()
        self.assertTrue(picking.tracking_notification_sent)
        self.assertEqual(action["params"]["type"], "info")

    # ---- Test 8: Biteship id present -> message carries track.biteship.com/{id} ----
    def test_08_tracking_url_from_biteship_id(self):
        picking = self._make_sale_picking()
        TRACK_SINGLE = (
            "odoo.addons.stock_picking_delivery_tracking.models."
            "stock_picking.StockPicking._track_single"
        )

        def fake_track_single():
            picking.write({"biteship_tracking_id": "abc123biteship"})
            return True

        with patch(TRACK_SINGLE, side_effect=fake_track_single), patch(
            SEND_MESSAGE,
            return_value={"ok": True},
        ) as mock_send:
            picking._notify_sales_telegram()

        mock_send.assert_called_once()
        chat_id, message = mock_send.call_args[0]
        self.assertEqual(chat_id, "987654321")
        self.assertIn("https://track.biteship.com/abc123biteship", message)

    # ---- Test 9: Biteship call raises -> notification still sent with fallback ----
    def test_09_biteship_failure_does_not_block_notification(self):
        picking = self._make_sale_picking()
        TRACK_SINGLE = (
            "odoo.addons.stock_picking_delivery_tracking.models."
            "stock_picking.StockPicking._track_single"
        )
        with patch(
            TRACK_SINGLE, side_effect=Exception("API down")
        ), patch(
            SEND_MESSAGE,
            return_value={"ok": True},
        ) as mock_send:
            picking._notify_sales_telegram()

        mock_send.assert_called_once()
        chat_id, message = mock_send.call_args[0]
        self.assertEqual(chat_id, "987654321")
        self.assertIn("(belum tersedia)", message)

    # ---- Test 10: carrier change re-sends notification with new courier ----
    def test_10_resend_on_carrier_change(self):
        picking = self._make_sale_picking()
        carrier2 = self.env["delivery.carrier"].create(
            {
                "name": "J&T",
                "delivery_type": "fixed",
                "product_id": self.env["product.product"]
                .create(
                    {
                        "name": "J&T Delivery",
                        "type": "service",
                        "list_price": 0,
                    }
                )
                .id,
            }
        )
        with patch(SEND_MESSAGE) as mock_send:
            picking.write({"carrier_tracking_ref": "JNE-ABC-8"})
            picking.write({"carrier_id": carrier2.id})

        self.assertEqual(mock_send.call_count, 2)
        second_message = mock_send.call_args_list[1].args[1]
        self.assertIn("J&T", second_message)
        picking.invalidate_recordset()
        self.assertTrue(picking.tracking_notification_sent)

    # ---- Test 11: link tracking tampil untuk semua pilihan carrier ----
    def test_11_tracking_url_present_for_every_carrier(self):
        carriers = ["JNE", "J&T", "Pos Indonesia", "Sicepat"]
        for name in carriers:
            carrier = self.env["delivery.carrier"].create(
                {
                    "name": name,
                    "delivery_type": "fixed",
                    "product_id": self.env["product.product"]
                    .create(
                        {
                            "name": "%s Delivery" % name,
                            "type": "service",
                            "list_price": 0,
                        }
                    )
                    .id,
                }
            )
            picking = self._make_sale_picking()
            picking.carrier_id = carrier
            with patch(SEND_MESSAGE):
                picking.write({"carrier_tracking_ref": "RESI-%s" % name})

            picking.invalidate_recordset()
            message = picking._build_tracking_notification_message()
            self.assertIn(
                picking.delivery_tracking_url,
                message,
                "Link tracking publik harus muncul untuk carrier %s" % name,
            )
            self.assertNotIn("(belum tersedia)", message)
