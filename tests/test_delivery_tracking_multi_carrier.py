# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

from unittest.mock import patch
from odoo.tests.common import TransactionCase


class TestDeliveryTrackingMultiCarrier(TransactionCase):
    """Test suite for multiple carriers and tracking lines."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Picking = cls.env["stock.picking"]
        cls.partner = cls.env["res.partner"].create({"name": "Multi Carrier Customer"})
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

    def test_01_create_with_carrier_and_ref_auto_populates_line(self):
        picking = self.Picking.create(
            {
                "picking_type_id": self.picking_type_out.id,
                "partner_id": self.partner.id,
                "carrier_id": self.carrier_jne.id,
                "carrier_tracking_ref": "JNE-1001",
            }
        )
        self.assertEqual(len(picking.carrier_tracking_line_ids), 1)
        line = picking.carrier_tracking_line_ids[0]
        self.assertEqual(line.carrier_id, self.carrier_jne)
        self.assertEqual(line.carrier_tracking_ref, "JNE-1001")

    def test_02_create_with_two_tracking_lines(self):
        picking = self.Picking.create(
            {
                "picking_type_id": self.picking_type_out.id,
                "partner_id": self.partner.id,
                "carrier_tracking_line_ids": [
                    (
                        0,
                        0,
                        {
                            "sequence": 10,
                            "carrier_id": self.carrier_jne.id,
                            "carrier_tracking_ref": "JNE-1002",
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "sequence": 20,
                            "carrier_id": self.carrier_jnt.id,
                            "carrier_tracking_ref": "JNT-2002",
                        },
                    ),
                ],
            }
        )
        self.assertEqual(len(picking.carrier_tracking_line_ids), 2)
        # picking carrier and tracking ref sync with the first line
        self.assertEqual(picking.carrier_id, self.carrier_jne)
        self.assertEqual(picking.carrier_tracking_ref, "JNE-1002")

        # Portal / public data contains both lines
        data = picking._get_delivery_tracking_data()
        self.assertIn("tracking_lines", data)
        self.assertEqual(len(data["tracking_lines"]), 2)
        self.assertEqual(data["tracking_lines"][0]["resi"], "JNE-1002")
        self.assertEqual(data["tracking_lines"][0]["courier_name"], "JNE")
        self.assertEqual(data["tracking_lines"][1]["resi"], "JNT-2002")
        self.assertEqual(data["tracking_lines"][1]["courier_name"], "J&T")

    def test_03_telegram_message_formats_two_couriers(self):
        picking = self.Picking.create(
            {
                "picking_type_id": self.picking_type_out.id,
                "partner_id": self.partner.id,
                "carrier_tracking_line_ids": [
                    (
                        0,
                        0,
                        {
                            "sequence": 10,
                            "carrier_id": self.carrier_jne.id,
                            "carrier_tracking_ref": "JNE-1003",
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "sequence": 20,
                            "carrier_id": self.carrier_jnt.id,
                            "carrier_tracking_ref": "JNT-2003",
                        },
                    ),
                ],
            }
        )
        msg = picking._build_tracking_notification_message()
        self.assertIn("Kurir 1: JNE", msg)
        self.assertIn("No. Resi: JNE-1003", msg)
        self.assertIn("Kurir 2: J&T", msg)
        self.assertIn("No. Resi: JNT-2003", msg)

    def test_04_editing_lines_regenerates_token_and_resets_status(self):
        picking = self.Picking.create(
            {
                "picking_type_id": self.picking_type_out.id,
                "partner_id": self.partner.id,
                "carrier_tracking_line_ids": [
                    (
                        0,
                        0,
                        {
                            "sequence": 10,
                            "carrier_id": self.carrier_jne.id,
                            "carrier_tracking_ref": "JNE-OLD",
                        },
                    ),
                ],
            }
        )
        old_token = picking.delivery_tracking_token
        picking.delivery_tracking_status = "shipped"

        # Update first line resi
        line = picking.carrier_tracking_line_ids[0]
        line.write({"carrier_tracking_ref": "JNE-NEW"})
        picking.invalidate_recordset()

        self.assertNotEqual(picking.delivery_tracking_token, old_token)
        self.assertEqual(picking.delivery_tracking_status, "draft")

    def test_05_track_single_aggregates_multiple_lines(self):
        picking = self.Picking.create(
            {
                "picking_type_id": self.picking_type_out.id,
                "partner_id": self.partner.id,
                "carrier_tracking_line_ids": [
                    (
                        0,
                        0,
                        {
                            "sequence": 10,
                            "carrier_id": self.carrier_jne.id,
                            "carrier_tracking_ref": "JNE-3001",
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "sequence": 20,
                            "carrier_id": self.carrier_jnt.id,
                            "carrier_tracking_ref": "JNT-3002",
                        },
                    ),
                ],
            }
        )
        provider = self.env["delivery.tracking.provider"].create(
            {
                "name": "Biteship Test Provider",
                "api_key": "biteship_test.123",
                "active": True,
            }
        )

        def mock_get_status(self_client, waybill_id, courier_code):
            if waybill_id == "JNE-3001":
                return {
                    "status": "delivered",
                    "status_desc": "Paket diterima",
                    "history": [],
                }
            else:
                return {
                    "status": "in_transit",
                    "status_desc": "Paket sedang diantar kurir lokal",
                    "history": [],
                }

        with patch(
            "odoo.addons.stock_picking_delivery_tracking.services.tracking_api_client.TrackingAPIClient.get_status",
            side_effect=mock_get_status,
        ):
            success = picking._track_single(provider)

        self.assertTrue(success)
        line1 = picking.carrier_tracking_line_ids[0]
        line2 = picking.carrier_tracking_line_ids[1]
        self.assertEqual(line1.delivery_tracking_status, "delivered")
        self.assertEqual(line2.delivery_tracking_status, "in_transit")
        # Overall status when one is in_transit and one is delivered is in_transit
        self.assertEqual(picking.delivery_tracking_status, "in_transit")
