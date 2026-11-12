/** @odoo-module **/

import { Component, useRef } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

/**
 * Read-only URL field with a "Salin" (copy) button that also works on plain
 * HTTP. The browser Clipboard API (`navigator.clipboard`) is only available in
 * a secure context (HTTPS or localhost), so Odoo's built-in CopyClipboardURL
 * silently does nothing on an http:// intranet address. This widget falls back
 * to the legacy `document.execCommand("copy")`, and the value is rendered in a
 * real <input> so it can always be selected and copied manually with Ctrl+C.
 */
export class CopyTrackingUrlField extends Component {
    setup() {
        this.input = useRef("input");
        this.notification = useService("notification");
    }

    selectAll() {
        const input = this.input.el;
        if (input) {
            input.focus();
            input.select();
        }
    }

    async copy() {
        const value = this.props.value || "";
        if (!value) {
            return;
        }
        let copied = false;

        // Preferred path: secure contexts (HTTPS / localhost).
        if (window.isSecureContext && navigator.clipboard) {
            try {
                await navigator.clipboard.writeText(value);
                copied = true;
            } catch {
                copied = false;
            }
        }

        // Fallback for plain HTTP intranet deployments.
        if (!copied) {
            const input = this.input.el;
            try {
                input.focus();
                input.select();
                input.setSelectionRange(0, value.length);
                copied = document.execCommand("copy");
            } catch {
                copied = false;
            }
            const selection = window.getSelection();
            if (selection) {
                selection.removeAllRanges();
            }
        }

        if (copied) {
            this.notification.add("Link tracking disalin.", { type: "success" });
        } else {
            this.notification.add(
                "Gagal menyalin otomatis. Pilih teks lalu tekan Ctrl+C.",
                { type: "warning" }
            );
        }
    }
}

CopyTrackingUrlField.template =
    "stock_picking_delivery_tracking.CopyTrackingUrlField";
CopyTrackingUrlField.props = { ...standardFieldProps };

registry.category("fields").add(
    "delivery_tracking_copy_url",
    CopyTrackingUrlField
);
