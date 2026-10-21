# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

import json

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase


class TestDeliveryTrackingToken(TransactionCase):
    """Token generation / regeneration rules for the public tracking link."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Picking = cls.env["stock.picking"]
        cls.partner = cls.env["res.partner"].create({"name": "Token Customer"})
        cls.picking_type_out = cls.env.ref("stock.picking_type_out")
        cls.carrier = cls.env["delivery.carrier"].create(
            {
                "name": "JNE",
                "delivery_type": "fixed",
                "biteship_courier_code": "jne",
                "product_id": cls.env["product.product"]
                .create({"name": "JNE Delivery", "type": "service", "list_price": 0})
                .id,
            }
        )

    def _make_picking(self, resi="JNE-TOK-1"):
        picking = self.Picking.create(
            {
                "picking_type_id": self.picking_type_out.id,
                "partner_id": self.partner.id,
                "carrier_id": self.carrier.id,
                "carrier_tracking_ref": resi,
            }
        )
        picking.state = "done"
        return picking

    def test_01_token_generated_on_create(self):
        p1 = self._make_picking("JNE-AAA")
        p2 = self._make_picking("JNE-BBB")
        self.assertTrue(p1.delivery_tracking_token)
        self.assertEqual(len(p1.delivery_tracking_token), 32)
        # distinct pickings never share a token
        self.assertNotEqual(p1.delivery_tracking_token, p2.delivery_tracking_token)
        # URL uses the token
        self.assertIn("/tracking/%s" % p1.delivery_tracking_token, p1.delivery_tracking_url)

    def test_02_token_regenerated_on_resi_change(self):
        picking = self._make_picking("JNE-OLD")
        old_token = picking.delivery_tracking_token
        picking.write({"carrier_tracking_ref": "JNE-NEW"})
        picking.invalidate_recordset()
        self.assertNotEqual(picking.delivery_tracking_token, old_token)

    def test_02b_token_not_regenerated_on_same_value(self):
        picking = self._make_picking("JNE-SAME")
        old_token = picking.delivery_tracking_token
        picking.write({"carrier_tracking_ref": "JNE-SAME"})
        picking.invalidate_recordset()
        self.assertEqual(picking.delivery_tracking_token, old_token)

    def test_02c_token_regenerated_on_carrier_change(self):
        picking = self._make_picking("JNE-CAR")
        old_token = picking.delivery_tracking_token
        carrier2 = self.carrier.copy()
        picking.write({"carrier_id": carrier2.id})
        picking.invalidate_recordset()
        self.assertNotEqual(picking.delivery_tracking_token, old_token)

    def test_03_legacy_picking_gets_token_on_demand(self):
        picking = self._make_picking("JNE-LEG")
        picking.write({"delivery_tracking_token": False})
        picking.invalidate_recordset()
        self.assertFalse(picking.delivery_tracking_token)
        picking._ensure_delivery_tracking_token()
        picking.invalidate_recordset()
        self.assertTrue(picking.delivery_tracking_token)


class TestDeliveryTrackingSteps(TransactionCase):
    """Timeline milestone derivation from internal state + Biteship history."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Picking = cls.env["stock.picking"]
        cls.partner = cls.env["res.partner"].create({"name": "Steps Customer"})
        cls.picking_type_out = cls.env.ref("stock.picking_type_out")
        cls.carrier = cls.env["delivery.carrier"].create(
            {
                "name": "J&T",
                "delivery_type": "fixed",
                "biteship_courier_code": "jnt",
                "product_id": cls.env["product.product"]
                .create({"name": "JNT Delivery", "type": "service", "list_price": 0})
                .id,
            }
        )

    def _make_picking(self, resi="STEPS-1", state="done"):
        picking = self.Picking.create(
            {
                "picking_type_id": self.picking_type_out.id,
                "partner_id": self.partner.id,
                "carrier_id": self.carrier.id,
                "carrier_tracking_ref": resi,
            }
        )
        picking.state = state
        return picking

    def test_01_fresh_picking_only_first_step(self):
        picking = self._make_picking("STEP-FRESH", state="draft")
        steps = picking._get_delivery_tracking_steps()
        self.assertEqual(len(steps), 6)
        self.assertTrue(steps[0]["reached"])  # Order Dibuat
        self.assertFalse(steps[1]["reached"])  # Diproses
        self.assertTrue(steps[1]["current"])  # currently being worked on

    def test_02_delivered_all_reached(self):
        picking = self._make_picking("STEP-DONE")
        picking.write({"delivery_tracking_status": "delivered"})
        steps = picking._get_delivery_tracking_steps()
        self.assertTrue(all(step["reached"] for step in steps))
        self.assertTrue(steps[-1]["current"])

    def test_03_exception_reported_no_current_step(self):
        picking = self._make_picking("STEP-EXC")
        picking.write({"delivery_tracking_status": "exception"})
        steps = picking._get_delivery_tracking_steps()
        self.assertFalse(steps[-1]["reached"])
        # no milestone pulses while the shipment is in exception
        self.assertFalse(any(step["current"] for step in steps))

    def test_04_droppingoff_log_marks_out_for_delivery(self):
        picking = self._make_picking("STEP-DROPOFF")
        self.env["delivery.tracking.log"].create(
            {
                "picking_id": picking.id,
                "status": "in_transit",
                "status_desc": "Kurir menuju alamat tujuan",
                "event_datetime": "2026-08-11 08:00:00",
                "raw_response": json.dumps({"status": "droppingOff"}),
            }
        )
        picking.delivery_tracking_status = "in_transit"
        steps = picking._get_delivery_tracking_steps()
        staying = {step["key"]: step["reached"] for step in steps}
        self.assertTrue(staying["out_for_delivery"])
        self.assertFalse(staying["delivered"])


class TestDeliveryTrackingPortalLogic(TransactionCase):
    """Portal access-control + public-token lookup — deterministic tests.

    Covers the same logic the controllers rely on:
    - public no-login route => sudo token search
    - portal login route    => owning-partner check via
      ``check_access_rights``/``check_access_rule`` (under ``portal`` the
      ``sale_order_personal_rule`` only lets the order owner read it).

    NOTE: a real-HTTP HttpCase round trip is intentionally *not* done here:
      under Odoo 16's ``--test-enable -u`` flow, module ``current_test`` is a
      plain bool during module load, which makes ``url_open`` on website
      routes crash in the test HTTP server (Odoo bug, unrelated to this
      module). Route reachability (200 / 404 / 302) is smoke-tested manually
      after deploy instead.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Picking = cls.env["stock.picking"]
        cls.SaleOrder = cls.env["sale.order"]
        cls.partner_a = cls.env["res.partner"].create({"name": "Portal Customer A"})
        cls.partner_b = cls.env["res.partner"].create({"name": "Portal Customer B"})
        cls.pricelist = cls.env["product.pricelist"].create({"name": "Portal PL"})
        cls.carrier = cls.env["delivery.carrier"].create(
            {
                "name": "Pos Indonesia",
                "delivery_type": "fixed",
                "product_id": cls.env["product.product"]
                .create({"name": "Pos Delivery", "type": "service", "list_price": 0})
                .id,
            }
        )

    def _portal_user(self, login, partner):
        return self.env["res.users"].create(
            {
                "name": login,
                "login": login,
                "password": "Pwd12345",
                "partner_id": partner.id,
                "groups_id": [(4, self.env.ref("base.group_portal").id)],
            }
        )

    def _sale_picking(self, partner):
        product = self.env["product.product"].create(
            {"name": "Portal Product", "type": "product"}
        )
        so = self.SaleOrder.create(
            {
                "partner_id": partner.id,
                "pricelist_id": self.pricelist.id,
                "order_line": [
                    (0, 0, {"product_id": product.id, "product_uom_qty": 1})
                ],
            }
        )
        so.message_subscribe(partner_ids=[partner.id])
        so.action_confirm()
        picking = so.picking_ids
        picking.carrier_id = self.carrier
        picking.write({"carrier_tracking_ref": "POS-PRT-1"})
        return so, picking

    def test_01_token_lookup_finds_picking(self):
        so, picking = self._sale_picking(self.partner_a)
        found = self.env["stock.picking"].sudo().search(
            [("delivery_tracking_token", "=", picking.delivery_tracking_token)]
        )
        self.assertEqual(found, picking)

    def test_02_token_lookup_unknown_token_empty(self):
        found = self.env["stock.picking"].sudo().search(
            [("delivery_tracking_token", "=", "no-such-token-value")]
        )
        self.assertFalse(found)

    def test_03_owner_read_access_ok(self):
        user_a = self._portal_user("portal_a", self.partner_a)
        so_a, picking_a = self._sale_picking(self.partner_a)
        env_a = self.env(user=user_a.id)
        order = env_a["sale.order"].browse(so_a.id)
        order.check_access_rights("read")
        order.check_access_rule("read")  # passes -> own order readable

    def test_04_other_partner_read_access_denied(self):
        user_b = self._portal_user("portal_b", self.partner_b)
        so_a, picking_a = self._sale_picking(self.partner_a)
        env_b = self.env(user=user_b.id)
        order = env_b["sale.order"].browse(so_a.id)
        with self.assertRaises(AccessError):
            order.check_access_rule("read")

    def test_05_tracking_page_data_is_tracking_only(self):
        so, picking = self._sale_picking(self.partner_a)
        data = picking._get_delivery_tracking_data()
        self.assertEqual(data["status"], picking.delivery_tracking_status)
        self.assertEqual(data["resi"], picking.carrier_tracking_ref)
        self.assertIsInstance(data["steps"], list)
        self.assertEqual(len(data["steps"]), 6)
        self.assertIn("courier_name", data)
        self.assertIn("tracking_url", data)
        # tracking page must not expose money / the order object itself
        self.assertNotIn("amount_total", data)

    def test_06_public_data_has_no_prices(self):
        so, picking = self._sale_picking(self.partner_a)
        values = picking._get_delivery_tracking_data()
        rendered = str(values)
        self.assertNotIn(str(so.amount_total), rendered)