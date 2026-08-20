# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from odoo.addons.stock_picking_delivery_tracking.services.tracking_api_client import (
    TrackingAPIClient,
)

# Path where `requests` is used inside the client. Patching this guarantees no
# real HTTP call ever reaches api.biteship.com during the test suite.
CLIENT_REQUESTS = (
    "odoo.addons.stock_picking_delivery_tracking."
    "services.tracking_api_client.requests"
)
# The model module imports TrackingAPIClient directly. We patch ONLY the
# ``get_status`` method on the real class so that ``_map_status`` (used when
# creating tracking logs) stays functional during cron/bulk tests.
REAL_CLIENT_PATH = (
    "odoo.addons.stock_picking_delivery_tracking."
    "services.tracking_api_client.TrackingAPIClient"
)
MODEL_CLIENT_GET_STATUS = (
    "odoo.addons.stock_picking_delivery_tracking."
    "models.stock_picking.TrackingAPIClient.get_status"
)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text if text is not None else json.dumps(json_data or {})

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


class TestTrackingAPIClient(TransactionCase):
    """Pure-Python client tests with mocked `requests` (no real HTTP)."""

    def _provider(self, **kwargs):
        data = {
            "api_base_url": "https://api.biteship.com",
            "api_key": "biteship_test.KEY",
            "auth_type": "api_key_header",
            "auth_header_name": "Authorization",
        }
        data.update(kwargs)
        return SimpleNamespace(**data)

    # ---- Test 1: _map_status (14 official + case variants + unknown) ----
    def test_map_status_official_and_unknown(self):
        cases = {
            "confirmed": "shipped",
            "allocated": "shipped",
            "pickingUp": "shipped",
            "picked": "shipped",
            "inTransit": "in_transit",
            "droppingOff": "in_transit",
            "onHold": "in_transit",
            "delivered": "delivered",
            "returnInTransit": "exception",
            "returned": "exception",
            "rejected": "exception",
            "courierNotFound": "exception",
            "cancelled": "exception",
            "disposed": "exception",
        }
        for biteship, expected in cases.items():
            self.assertEqual(TrackingAPIClient._map_status(biteship), expected)

    def test_map_status_case_insensitive(self):
        for src in ("INTRANSIT", "Delivered", "PICKED", "OnHold"):
            self.assertEqual(
                TrackingAPIClient._map_status(src),
                {
                    "INTRANSIT": "in_transit",
                    "Delivered": "delivered",
                    "PICKED": "shipped",
                    "OnHold": "in_transit",
                }[src],
            )

    def test_map_status_unknown_fallback(self):
        self.assertEqual(TrackingAPIClient._map_status("someWeirdStatus"), "unknown")
        self.assertEqual(TrackingAPIClient._map_status(""), "unknown")
        self.assertEqual(TrackingAPIClient._map_status(None), "unknown")

    # ---- Test 6: get_status with mocked requests ----
    def test_get_status_success(self):
        payload = {
            "success": True,
            "waybill_id": "JNE123",
            "courier": {"company": "JNE"},
            "status": "inTransit",
            "history": [
                {"note": "Paket diterima JNE", "updated_at": "2026-08-10T10:00:00", "status": "confirmed"},
                {"note": "Dalam perjalanan", "updated_at": "2026-08-11T08:00:00+07:00", "status": "inTransit"},
            ],
        }
        client = TrackingAPIClient(self._provider())
        with patch(CLIENT_REQUESTS + ".get", return_value=FakeResponse(200, payload)) as m:
            result = client.get_status("JNE123", "jne")

        m.assert_called_once_with(
            "https://api.biteship.com/v1/trackings/JNE123/couriers/jne",
            headers={"Authorization": "biteship_test.KEY"},
            timeout=10,
        )
        self.assertEqual(result["status"], "in_transit")
        self.assertEqual(result["status_desc"], "Dalam perjalanan")
        self.assertEqual(len(result["history"]), 2)
        self.assertIsNone(result["location"])

    def test_get_status_timeout_then_retry_success(self):
        calls = {"n": 0}

        def fake_get(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("timed out")
            return FakeResponse(
                200,
                {
                    "success": True,
                    "status": "delivered",
                    "history": [
                        {"note": "Terkirim", "updated_at": "2026-08-12T12:00:00", "status": "delivered"}
                    ],
                },
            )

        client = TrackingAPIClient(self._provider(), retry_delay=0)
        with patch(
            CLIENT_REQUESTS + ".Timeout", TimeoutError
        ), patch(CLIENT_REQUESTS + ".get", side_effect=fake_get):
            result = client.get_status("JNE123", "jne")
        self.assertEqual(calls["n"], 2)
        self.assertEqual(result["status"], "delivered")

    def test_get_status_auth_error_no_raise(self):
        client = TrackingAPIClient(self._provider())
        with patch(CLIENT_REQUESTS + ".get", return_value=FakeResponse(401, text="unauthorized")):
            result = client.get_status("JNE123", "jne")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error_code"], "auth_error")
        self.assertIn("error", result)

    def test_get_status_success_false_handled_as_error(self):
        payload = {"success": False, "message": "Waybill not found"}
        client = TrackingAPIClient(self._provider())
        with patch(CLIENT_REQUESTS + ".get", return_value=FakeResponse(200, payload)):
            result = client.get_status("BOGUS", "jne")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error_code"], "api_error")
        self.assertIn("Waybill not found", result["error"])

    def test_get_status_missing_args(self):
        client = TrackingAPIClient(self._provider())
        with patch(CLIENT_REQUESTS + ".get") as m:
            result = client.get_status("", "jne")
        m.assert_not_called()
        self.assertEqual(result["error_code"], "invalid_arguments")

    def test_get_status_http_400_keeps_body_error(self):
        """Non-200 responses must carry the API error detail, not just the code."""
        payload = {"success": False, "error": "Data is not found", "code": 40003003}
        client = TrackingAPIClient(self._provider())
        with patch(CLIENT_REQUESTS + ".get", return_value=FakeResponse(400, payload)):
            result = client.get_status("BOGUS", "jne")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error_code"], "http_error")
        self.assertIn("Data is not found", result["error"])
        self.assertIn("HTTP 400", result["error"])

    # ---- Test 7: timestamp parsing with tz offset (dateutil regression) ----
    def test_parse_event_datetime_timezone(self):
        picking = self.env["stock.picking"]
        parsed = picking._parse_event_datetime("2026-08-11T08:00:00+07:00")
        self.assertEqual(parsed, datetime(2026, 8, 11, 1, 0, 0))
        self.assertIsNone(parsed.tzinfo)

    def test_parse_event_datetime_plain(self):
        picking = self.env["stock.picking"]
        parsed = picking._parse_event_datetime("2026-08-10T10:00:00")
        self.assertEqual(parsed, datetime(2026, 8, 10, 10, 0, 0))

    def test_parse_event_datetime_invalid(self):
        picking = self.env["stock.picking"]
        self.assertFalse(picking._parse_event_datetime("not-a-date"))
        self.assertFalse(picking._parse_event_datetime(None))


class TestStockPickingTracking(TransactionCase):
    """Cron, dedupe, manual & bulk actions — all HTTP mocked."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Picking = cls.env["stock.picking"]
        cls.Provider = cls.env["delivery.tracking.provider"]
        cls.Carrier = cls.env["delivery.carrier"]
        cls.Icp = cls.env["ir.config_parameter"]

        cls.provider = cls.Provider.create(
            {
                "name": "Biteship Test",
                "api_base_url": "https://api.biteship.com",
                "api_key": "biteship_test.KEY",
            }
        )
        cls.picking_type_out = cls.env.ref("stock.picking_type_out")
        cls.partner = cls.env["res.partner"].create({"name": "Test Customer"})

    def _make_carrier(self, name="JNE", courier_code="jne"):
        return self.Carrier.create(
            {
                "name": name,
                "delivery_type": "fixed",
                "biteship_courier_code": courier_code,
                "product_id": self.env["product.product"]
                .create({"name": name + " Delivery", "type": "service", "list_price": 0})
                .id,
            }
        )

    def _make_picking(
        self,
        resi="JNE-1",
        state="done",
        status=None,
        last_check=None,
        carrier=None,
    ):
        picking = self.Picking.create(
            {
                "picking_type_id": self.picking_type_out.id,
                "partner_id": self.partner.id,
                "carrier_id": carrier.id if carrier else False,
                "carrier_tracking_ref": resi,
                "delivery_tracking_status": status,
                "delivery_last_check": last_check,
            }
        )
        picking.state = state
        return picking

    def _ok_result(self, status="in_transit", desc="Dalam perjalanan", updated_at=None, history=None):
        updated_at = updated_at or "2026-08-11T08:00:00+07:00"
        history = history or [
            {"note": "Paket diterima JNE", "updated_at": "2026-08-10T10:00:00", "status": "confirmed"},
            {"note": desc, "updated_at": updated_at, "status": status},
        ]
        return {
            "status": status,
            "status_desc": desc,
            "location": None,
            "event_datetime": updated_at,
            "history": history,
            "raw_response": {"success": True, "history": history},
        }

    # ---- Test 2: cron eligibility, batch limit, ordering ----
    def test_cron_only_processes_eligible(self):
        carrier = self._make_carrier()
        # eligible
        good = self._make_picking(resi="ELIG-1", carrier=carrier)
        # not eligible: no resi
        self._make_picking(resi="", carrier=carrier)
        # not eligible: already delivered (final)
        self._make_picking(resi="DONE-1", status="delivered", carrier=carrier)
        # not eligible: exception (final)
        self._make_picking(resi="EXC-1", status="exception", carrier=carrier)
        # not eligible: not done state
        self._make_picking(resi="DRAFT-1", state="draft", carrier=carrier)
        # not eligible: incoming picking type
        incoming = self.Picking.create(
            {
                "picking_type_id": self.env.ref("stock.picking_type_in").id,
                "partner_id": self.partner.id,
                "carrier_id": carrier.id,
                "carrier_tracking_ref": "IN-1",
            }
        )
        incoming.state = "done"

        waybills = []

        def fake_get_status(waybill, courier):
            waybills.append(waybill)
            return self._ok_result()

        with patch(MODEL_CLIENT_GET_STATUS, side_effect=fake_get_status):
            self.Picking.cron_check_delivery_status()

        self.assertIn("ELIG-1", waybills)
        for not_eligible in ("", "DONE-1", "EXC-1", "DRAFT-1", "IN-1"):
            self.assertNotIn(not_eligible, waybills)
        good.invalidate_recordset()
        self.assertEqual(good.delivery_tracking_status, "in_transit")

    def test_cron_batch_limit_from_param(self):
        carrier = self._make_carrier()
        for i in range(5):
            self._make_picking(resi="B-%d" % i, carrier=carrier)

        waybills = []

        def fake_get_status(waybill, courier):
            waybills.append(waybill)
            return self._ok_result()

        self.Icp.sudo().set_param("delivery_tracking_cron_batch_size", "2")
        with patch(MODEL_CLIENT_GET_STATUS, side_effect=fake_get_status):
            self.Picking.cron_check_delivery_status()
        self.assertEqual(len(waybills), 2)
        self.Icp.sudo().set_param("delivery_tracking_cron_batch_size", "50")

    def test_cron_orders_oldest_first_nulls_first(self):
        # Regression test for the Fase 3 NULLS FIRST bug: pickings never
        # checked (NULL last_check) must be processed before older-checked ones.
        carrier = self._make_carrier()
        older = self._make_picking(
            resi="OLD-1",
            carrier=carrier,
            last_check=datetime(2026, 7, 1, 12, 0, 0),
        )
        never_checked = self._make_picking(resi="NEVER-1", carrier=carrier)
        newer = self._make_picking(
            resi="NEW-1",
            carrier=carrier,
            last_check=datetime(2026, 8, 1, 12, 0, 0),
        )

        waybills = []

        def fake_get_status(waybill, courier):
            waybills.append(waybill)
            return self._ok_result()

        self.Icp.sudo().set_param("delivery_tracking_cron_batch_size", "3")
        with patch(MODEL_CLIENT_GET_STATUS, side_effect=fake_get_status):
            self.Picking.cron_check_delivery_status()
        self.Icp.sudo().set_param("delivery_tracking_cron_batch_size", "50")

        # never_checked (NULL) first, then older, then newer
        self.assertEqual(waybills, ["NEVER-1", "OLD-1", "NEW-1"])

    # ---- Test 3: log dedupe ----
    def test_log_dedupe(self):
        carrier = self._make_carrier()
        picking = self._make_picking(resi="DEDUPE-1", carrier=carrier)

        history = [
            {"note": "Paket diterima JNE", "updated_at": "2026-08-10T10:00:00", "status": "confirmed"},
            {"note": "Dalam perjalanan", "updated_at": "2026-08-11T08:00:00+07:00", "status": "inTransit"},
        ]
        # 1st poll: 2 new logs
        picking._process_tracking_result(self._ok_result(history=history))
        self.assertEqual(len(picking.tracking_log_ids), 2)

        # 2nd poll with identical history: 0 new logs
        picking._process_tracking_result(self._ok_result(history=history))
        self.assertEqual(len(picking.tracking_log_ids), 2)

        # 3rd poll: history +1 new entry -> exactly 1 new log
        history_3 = history + [
            {"note": "Terkirim", "updated_at": "2026-08-12T12:00:00+07:00", "status": "delivered"}
        ]
        picking._process_tracking_result(self._ok_result(history=history_3))
        self.assertEqual(len(picking.tracking_log_ids), 3)

        # Old entries not re-created
        notes = picking.tracking_log_ids.mapped("status_desc")
        self.assertEqual(notes.count("Paket diterima JNE"), 1)
        self.assertEqual(notes.count("Dalam perjalanan"), 1)
        self.assertEqual(notes.count("Terkirim"), 1)

    # ---- Test 3b: error message persisted when status becomes unknown ----
    def test_tracking_error_persisted_on_failure(self):
        carrier = self._make_carrier()
        picking = self._make_picking(resi="ERR-PERSIST-1", carrier=carrier)

        error_result = {
            "status": "unknown",
            "status_desc": None,
            "location": None,
            "event_datetime": None,
            "history": [],
            "error": "HTTP 400: Data is not found (code 40003003)",
            "raw_response": {"success": False, "error": "Data is not found"},
        }
        picking._process_tracking_result(error_result)
        picking.invalidate_recordset()
        self.assertEqual(picking.delivery_tracking_status, "unknown")
        self.assertEqual(
            picking.delivery_tracking_error, "HTTP 400: Data is not found (code 40003003)"
        )
        # no history -> no logs, but the error is visible on the picking
        self.assertEqual(len(picking.tracking_log_ids), 0)

        # success later clears the error field
        picking._process_tracking_result(self._ok_result())
        picking.invalidate_recordset()
        self.assertEqual(picking.delivery_tracking_status, "in_transit")
        self.assertFalse(picking.delivery_tracking_error)


    # ---- Test 4: manual action ----
    def test_action_check_delivery_status_success(self):
        carrier = self._make_carrier()
        picking = self._make_picking(resi="MANUAL-1", carrier=carrier)

        with patch(MODEL_CLIENT_GET_STATUS, return_value=self._ok_result()):
            action = picking.action_check_delivery_status()

        picking.invalidate_recordset()
        self.assertEqual(picking.delivery_tracking_status, "in_transit")
        self.assertEqual(len(picking.tracking_log_ids), 2)
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(action["params"]["type"], "success")

    def test_action_check_delivery_status_unmapped_courier(self):
        carrier = self._make_carrier("Unmapped Carrier", courier_code=False)
        picking = self._make_picking(resi="UNMAP-1", carrier=carrier)
        with self.assertRaises(UserError) as cm:
            picking.action_check_delivery_status()
        self.assertIn("Biteship", str(cm.exception))
        self.assertIn("not mapped", str(cm.exception))

    # ---- Test 5: bulk action ----
    def test_bulk_more_than_20_raises_and_calls_no_api(self):
        carrier = self._make_carrier()
        pickings = self.Picking
        for i in range(21):
            pickings |= self._make_picking(resi="BULK-%d" % i, carrier=carrier)

        with patch(MODEL_CLIENT_GET_STATUS) as m:
            with self.assertRaises(UserError) as cm:
                pickings.action_refresh_selected_delivery_status()
        self.assertIn("20", str(cm.exception))
        m.assert_not_called()

    def test_bulk_mixed_success_failure_aggregates(self):
        mapped = self._make_carrier("Mapped", courier_code="jne")
        unmapped = self._make_carrier("Unmapped", courier_code=False)
        ok_picking = self._make_picking(resi="OK-1", carrier=mapped)
        fail_picking = self._make_picking(resi="FAIL-1", carrier=unmapped)

        with patch(MODEL_CLIENT_GET_STATUS, return_value=self._ok_result()) as m:
            action = (ok_picking | fail_picking).action_refresh_selected_delivery_status()

        ok_picking.invalidate_recordset()
        self.assertEqual(ok_picking.delivery_tracking_status, "in_transit")
        self.assertEqual(action["params"]["type"], "warning")
        self.assertIn("1 berhasil", action["params"]["message"])
        self.assertIn("1 gagal", action["params"]["message"])
        # failed record did not stop processing of the successful one
        self.assertEqual(action["params"]["message"].count("mapping"), 1)

    def test_bulk_exactly_20_ok(self):
        carrier = self._make_carrier()
        pickings = self.Picking
        for i in range(20):
            pickings |= self._make_picking(resi="B20-%d" % i, carrier=carrier)

        with patch(MODEL_CLIENT_GET_STATUS, return_value=self._ok_result()) as m:
            action = pickings.action_refresh_selected_delivery_status()

        self.assertEqual(action["params"]["type"], "success")
        self.assertEqual(m.call_count, 20)


class TestDashboardFilter(TransactionCase):
    """Optional: dashboard default filters actually exclude old/no-resi DOs."""

    def test_90_day_and_resi_filter(self):
        # Build the same domain the dashboard action uses
        # (static part: outgoing + resi terisi), then apply the 90-day window.
        carrier = self.env["delivery.carrier"].create(
            {
                "name": "JNE Dash",
                "delivery_type": "fixed",
                "biteship_courier_code": "jne",
                "product_id": self.env["product.product"]
                .create({"name": "JNE Dash", "type": "service", "list_price": 0})
                .id,
            }
        )
        out_type = self.env.ref("stock.picking_type_out")

        recent = self.env["stock.picking"].create(
            {
                "picking_type_id": out_type.id,
                "partner_id": self.env["res.partner"].create({"name": "C1"}).id,
                "carrier_id": carrier.id,
                "carrier_tracking_ref": "DASH-RECENT",
            }
        )
        recent.state = "done"
        recent.date_done = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        old = self.env["stock.picking"].create(
            {
                "picking_type_id": out_type.id,
                "partner_id": self.env["res.partner"].create({"name": "C2"}).id,
                "carrier_id": carrier.id,
                "carrier_tracking_ref": "DASH-OLD",
            }
        )
        old.state = "done"
        old.date_done = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d %H:%M:%S")

        no_resi = self.env["stock.picking"].create(
            {
                "picking_type_id": out_type.id,
                "partner_id": self.env["res.partner"].create({"name": "C3"}).id,
                "carrier_id": carrier.id,
            }
        )
        no_resi.state = "done"

        action = self.env.ref(
            "stock_picking_delivery_tracking.action_delivery_tracking_dashboard"
        )
        self.assertIn("'outgoing'", action.domain)
        self.assertIn("carrier_tracking_ref", action.domain)

        cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        domain = [
            ("picking_type_code", "=", "outgoing"),
            ("carrier_tracking_ref", "!=", False),
            ("date_done", ">=", cutoff),
        ]
        visible = self.env["stock.picking"].search(domain)
        self.assertIn(recent, visible)
        self.assertNotIn(old, visible)
        self.assertNotIn(no_resi, visible)

class TestAutoTrackToggle(TransactionCase):
    """The Auto Track via Biteship toggle must persist and read back its value
    through the cron record referenced by its xmlid."""

    def _cron(self):
        return self.env.ref(
            "stock_picking_delivery_tracking.cron_delivery_tracking_status_poll"
        )

    def test_toggle_saves_and_reads_back(self):
        settings = (
            self.env["res.config.settings"]
            .create({})
        )
        settings.delivery_tracking_auto_track_enabled = True
        settings.execute()
        self.assertTrue(self._cron().active)

        settings = self.env["res.config.settings"].create({})
        self.assertTrue(settings.get_values()["delivery_tracking_auto_track_enabled"])

        settings2 = (
            self.env["res.config.settings"]
            .create({})
        )
        settings2.delivery_tracking_auto_track_enabled = False
        settings2.execute()
        self.assertFalse(self._cron().active)

        settings3 = self.env["res.config.settings"].create({})
        self.assertFalse(settings3.get_values()["delivery_tracking_auto_track_enabled"])
