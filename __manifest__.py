# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

{
    "name": "Stock Picking Delivery Tracking",
    "version": "16.0.1.0.0",
    "summary": "Delivery status tracking via logistics API",
    "category": "Inventory",
    "author": "PT Aston Graphindo Indonesia",
    "website": "https://www.astongraphindo.com",
    "depends": ["stock", "delivery", "base_setup"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron_data.xml",
        "views/stock_picking_views.xml",
        "views/delivery_tracking_dashboard.xml",
        "views/res_config_settings_views.xml",
    ],
    "demo": [],
    "installable": True,
    "auto_install": False,
    "application": False,
    "license": "LGPL-3",
}
