# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

from unittest.mock import patch
from odoo.tests.common import TransactionCase


class TestDeliveryTrackingTwoCarriers(TransactionCase):
    """Test suite for 2 carriers & 2 tracking references."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Picking = cls.env["stock.picking"]
        cls.partner = cls.env["res.partner"].create({"name": "Two Carrier Customer"})
        cls.picking_type_out = cls.env.ref("stock.picking_type_out")
        cls.carrier_jne = cls.env["delivery.carrier"].create(
            {
                "name": "JNE",
                "delivery_type": "fixed",
                "biteship_courier_code": "jne",
                "product_id": cls.env["product.product"]
                .create({"name": "JNE Service", "type": "service", "list_price": 0})
                .id,
            }
        )
        cls.carrier_jnt = cls.env["delivery.carrier"].create(
            {
                "name": "J&T",
                "delivery_type": "fixed",
                "biteship_courier_code": "jnt",
                "product_id": cls.env["product.product"]
                .create({"name": "J&T Service", "type": "service", "list_price": 0})
                .id,
            }
        )

    def test_01_two_carriers_tokens_and_urls(self):
        picking = self.Picking.create(
            {
                "picking_type_id": self.picking_type_out.id,
                "partner_id": self.partner.id,
                "carrier_id": self.carrier_jne.id,
                "carrier_tracking_ref": "JNE-1001",
                "carrier_id_2": self.carrier_jnt.id,
                "carrier_tracking_ref_2": "JNT-2002",
            }
        )
        self.assertTrue(picking.delivery_tracking_token)
        self.assertTrue(picking.delivery_tracking_token_2)
        self.assertNotEqual(picking.delivery_tracking_token, picking.delivery_tracking_token_2)
        self.assertIn("/tracking/%s" % picking.delivery_tracking_token, picking.delivery_tracking_url)
        self.assertIn("/tracking/%s" % picking.delivery_tracking_token_2, picking.delivery_tracking_url_2)

    def test_02_telegram_notification_includes_both_couriers(self):
        picking = self.Picking.create(
            {
                "picking_type_id": self.picking_type_out.id,
                "partner_id": self.partner.id,
                "carrier_id": self.carrier_jne.id,
                "carrier_tracking_ref": "JNE-1001",
                "carrier_id_2": self.carrier_jnt.id,
                "carrier_tracking_ref_2": "JNT-2002",
            }
        )
        msg = picking._build_tracking_notification_message()
        self.assertIn("Kurir 1: JNE", msg)
        self.assertIn("No. Resi 1: JNE-1001", msg)
        self.assertIn("Kurir 2: J&T", msg)
        self.assertIn("No. Resi 2: JNT-2002", msg)
        self.assertIn("Link Tracking 1:", msg)
        self.assertIn("Link Tracking 2:", msg)

    def test_03_get_delivery_tracking_data_per_carrier(self):
        picking = self.Picking.create(
            {
                "picking_type_id": self.picking_type_out.id,
                "partner_id": self.partner.id,
                "carrier_id": self.carrier_jne.id,
                "carrier_tracking_ref": "JNE-1001",
                "carrier_id_2": self.carrier_jnt.id,
                "carrier_tracking_ref_2": "JNT-2002",
            }
        )
        data1 = picking._get_delivery_tracking_data(carrier_num=1)
        self.assertEqual(data1["courier_name"], "JNE")
        self.assertEqual(data1["resi"], "JNE-1001")
        self.assertEqual(data1["other_carrier"]["label"], "Kurir 2")
        self.assertEqual(data1["other_carrier"]["resi"], "JNT-2002")

        data2 = picking._get_delivery_tracking_data(carrier_num=2)
        self.assertEqual(data2["courier_name"], "J&T")
        self.assertEqual(data2["resi"], "JNT-2002")
        self.assertEqual(data2["other_carrier"]["label"], "Kurir 1")
        self.assertEqual(data2["other_carrier"]["resi"], "JNE-1001")
