# Copyright 2026 PT Aston Graphindo Indonesia
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

{
    "name": "Stock Picking Delivery Tracking",
    "version": "16.0.1.5.0",
    "summary": "Delivery status tracking via logistics API",
    "category": "Inventory",
    "author": "PT Aston Graphindo Indonesia",
    "website": "https://www.astongraphindo.com",
    "depends": ["stock", "delivery", "base_setup", "portal", "sale"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron_data.xml",
        "views/stock_picking_views.xml",
        "views/delivery_tracking_portal_templates.xml",
        "views/delivery_tracking_dashboard.xml",
        "views/res_users_view.xml",
        "views/res_config_settings_views.xml",
    ],
    "assets": {
        "web.assets_frontend": [
            "/stock_picking_delivery_tracking/static/src/scss"
            "/delivery_tracking_portal.scss",
        ],
        "web.assets_backend": [
            "/stock_picking_delivery_tracking/static/src/scss"
            "/delivery_tracking_backend.scss",
        ],
    },
    "demo": [],
    "installable": True,
    "auto_install": False,
    "application": False,
    "license": "LGPL-3",
}
