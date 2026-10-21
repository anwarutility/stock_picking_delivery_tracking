# Changelog - Stock Picking Delivery Tracking

## v16.0.1.5.0 — 2026-09-15: Customer Tracking Portal

### Fitur
Halaman portal customer-facing untuk melihat status pengiriman order secara
visual (stepper/timeline), tanpa mengubah logic Biteship webhook / Telegram.

#### 1. Akses Login (Portal)
- Route baru `/my/orders/<order_id>/tracking` (auth=user, website=True).
- Akses dicek lewat `_document_check_access('sale.order', ...)` — customer hanya
  bisa melihat order miliknya sendiri (partner_b tidak bisa akses order partner_a).
- Kolom **Tracking** baru di halaman `/my/orders` (inherit `sale.portal_my_orders`)
  yang menautkan ke halaman tracking per order.

#### 2. Akses Publik (Token Link)
- Route `/tracking/<token>` (auth=public) tanpa login — untuk dikirim via
  WA/email ke customer yang tidak punya akun portal.
- Token: `uuid4().hex` (128-bit), non-predictable, di-generate otomatis saat
  picking dibuat. **Regenerate otomatis** saat kurir atau nomor resi diubah.
- Halaman publik hanya menampilkan data tracking (resi, kurir, ETA, timeline,
  riwayat) — tidak ada harga / data pelanggan lain.

#### 3. Timeline / Stepper
- 6 milestone: Order Dibuat → Diproses → Dikirim → Dalam Perjalanan →
  Diantar → Selesai, plus banner khusus untuk status `exception`/`unknown`.
- `Diantar` dideteksi dari status mentah Biteship `droppingoff` di
  `raw_response` log (dipetakan ke `in_transit` di model).
- Timestamp tiap milestone diambil dari `delivery.tracking.log`
  (event_datetime ter-awal per status) dengan fallback date_done / last_check.
- Responsive: horizontal (desktop) / vertikal (≤620px, mobile).
- Riwayat status detail dalam elemen `<details>` (expand tanpa JS).

#### 4. Backend
- Field `delivery_tracking_url` (computed) pada form `stock.picking` dengan
  widget `CopyClipboardURL` = **tombol salin link tracking** untuk sales/CS.
- Field `delivery_tracking_token` (Char, default uuid4).

#### 5. Migration (`migrations/16.0.1.5.0/post-migrate.py`)
- Backfill `delivery_tracking_token` untuk DO existing (gen_random_uuid).

#### 6. Fix pasca-deploy
- **Register controller portal**: `__init__.py` modul kini juga meng-import
  `controllers` (sebelumnya hanya `models` + `services`, sehingga rute
  `/tracking/<token>` dan `/my/orders/<order_id>/tracking` tak terdaftar dan
  mengembalikan 404).
- **Aset SCSS**: dipindah dari `inherit_id="web.assets_frontend"/"web.assets_backend"`
  (template record tidak ada di Odoo 16 → `ValueError: External ID not found`)
  ke key `assets` di `__manifest__.py`, mengikuti pola module `portal`.
- **Token duplikat pasca-migration**: DB prod sempat berisi satu token sama
  untuk semua DO (sumber nilai hardcoded lama). Data di `agi` dan `agi_test`
  di-fix manual (regenerate `gen_random_uuid()` per-baris); migration 1.5.0
  terbukti menghasilkan token unik.
- **404 rute publik**: server tanpa `dbfilter` tidak dapat memilih DB untuk
  request tanpa sesi login — ditambahkan `dbfilter = ^agi$` di `/etc/odoo/odoo.conf`.

> **Breaking Change (disengaja) — dbfilter server-wide**: server `agi-ai`
> menjalankan **satu** instans Odoo (port 8069) dengan db produksi `agi` dan
> replika test `agi_test` (db `odoo`/`postgres` bukan instans Odoo). `agi_test`
> tidak dipakai via browser. Dengan `dbfilter = ^agi$`, **semua** hostname
> diarahkan ke `agi`: halaman selector multi-DB (`/web/database/selector`)
> tidak lagi muncul, dan akses browser ke `agi_test` tidak dimungkinkan
> (tetap bisa diakses via CLI `-d agi_test` untuk testing/upgrade). Ini
> diambil agar **link publik tanpa login** (`/tracking/<token>`) dapat resolve
> DB tanpa sesi. Jika di masa depan muncul db produksi kedua di server ini,
> ganti ke host-based filtering (`dbfilter = %h` + virtual-host per DB).

### Files Changed
| File | Change |
|------|--------|
| `models/stock_picking.py` | Token field, URL compute, `_get_delivery_tracking_steps`, `_get_delivery_tracking_data`, token regenerate di `write()` |
| `__init__.py` | **import `controllers`** (fix: rute portal terdaftar) |
| `__manifest__.py` | Key `assets` untuk SCSS frontend/backend (fix Odoo 16) |
| `controllers/delivery_tracking_portal.py` | Route login + route publik token |
| `controllers/__init__.py` | Register controller |
| `views/delivery_tracking_portal_templates.xml` | Halaman tracking, assets, inherit `/my/orders` |
| `views/stock_picking_views.xml` | Field `delivery_tracking_url` widget CopyClipboardURL |
| `static/src/scss/delivery_tracking_portal.scss` | Style stepper (frontend) |
| `static/src/scss/delivery_tracking_backend.scss` | Cosmetic backend |
| `migrations/16.0.1.5.0/post-migrate.py` | Backfill token existing |
| `tests/test_delivery_tracking_portal.py` | Test token, steps, HTTP routes |
| `tests/__init__.py` | Register test baru |
| `__manifest__.py` | Version `16.0.1.5.0`, depends +portal +sale |

### Keamanan
- Route publik: read-only (sudo), hanya field tracking, tanpa rate-limit
  terpisah — token 128-bit membuat enumerasi tidak praktis.
- Route login: `_document_check_access` memastikan pemilik order.

## v16.0.1.4.0 — 2026-09-15: Fix resend Telegram saat resi / kurir diubah

### Bug
Deteksi "resi/kurir berubah" di `write()` memakai `picking._origin`, padahal di
Odoo 16 `_origin` untuk record DB normal mengembalikan record yang SAMA (cache
yang sama) — bukan snapshot nilai sebelum write. Akibatnya `old_ref != new_ref`
selalu `False`, sehingga reset status tracking dan notifikasi ulang TIDAK pernah
terjadi saat resi atau kurir diubah.

### Fix (`models/stock_picking.py`)
- Nilai lama (`carrier_tracking_ref`, `carrier_id`) disnapshot ke dict SEBELUM
  `super().write(vals)`, lalu dibandingkan dengan nilai baru setelah write.
- Saat nilai berubah: status tracking di-reset ke `draft` dan notifikasi
  Telegram terkirim ulang ke salesperson dengan info resi/kurir terbaru.
- Menulis ulang nilai yang SAMA tidak memicu notifikasi ulang (anti double-send).

### Behavior
- Input resi pertama → notifikasi 1x.
- Resi diubah → notifikasi ulang dengan resi baru.
- Kurir diubah → notifikasi ulang dengan kurir baru.

### Tests
- `test_02_resend_on_resi_change`: resi diubah → `_send_telegram_message`
  terpanggil 2x, pesan kedua berisi resi baru.
- `test_02b_no_double_send_on_same_value`: nilai sama ditulis ulang → tetap 1x.
- `test_08_resend_on_carrier_change`: kurir diubah → 2x, pesan kedua berisi
  nama kurir baru.

### Files Changed
| File | Change |
|------|--------|
| `models/stock_picking.py` | Snapshot nilai lama sebelum write, fix deteksi perubahan |
| `tests/test_delivery_tracking_telegram.py` | Update + tambah test resend |
| `__manifest__.py` | Version bump ke `16.0.1.4.0` |

## v16.0.1.3.0 — 2026-09-15: Editable Carrier After Validation

### Problem
Field **Kurir** (`carrier_id`) dibuat readonly oleh base Odoo `delivery` module
saat DO sudah di-validasi (`state = done` / `cancel`). Jika kurir yang dipakai
ternyata berbeda (misal DO diproseskan ke kurir lain), user tidak bisa
mengganti kurir meskipun nomor resi sudah bisa diedit.

### Solution

#### 1. View Override (`views/stock_picking_views.xml`)
- View `view_picking_form_carrier_tracking_ref_editable` diupdate:
  - `carrier_id` juga dibuat editable (tidak readonly) untuk semua state,
    sama seperti `carrier_tracking_ref`.

#### 2. Write Method Update (`models/stock_picking.py`)
- Method `write()` sekarang mendeteksi perubahan pada `carrier_id` ATAU
  `carrier_tracking_ref`.
- Saat kurir berubah, otomatis:
  - `delivery_tracking_status` di-reset ke `draft`
  - `delivery_tracking_status_desc` di-clear
  - `delivery_tracking_error` di-clear
  - `delivery_last_check` di-reset
  - `tracking_notification_sent` di-reset (supaya notifikasi Telegram baru
    terkirim dengan nama kurir yang benar)

### Behavior After Change
- User bisa mengganti kurir kapan saja, termasuk setelah DO validated.
- Saat kurir diubah, tracking akan dimulai ulang memakai Biteship courier
  code dari kurir baru.
- Notifikasi Telegram akan terkirim ulang ke salesperson dengan info kurir
  yang benar.

### Files Changed
| File | Change |
|------|--------|
| `views/stock_picking_views.xml` | `carrier_id` dibuat editable (priority 99) |
| `models/stock_picking.py` | Update `write()` — reset tracking saat kurir/resi berubah |
| `__manifest__.py` | Version bump ke `16.0.1.3.0` |

## v16.0.1.2.0 — 2026-09-08: Editable Resi After Validation

### Problem
Field **No. Resi** (`carrier_tracking_ref`) dibuat readonly oleh base Odoo `delivery` module
saat DO sudah di-validasi (`state = done` / `cancel`). Jika resi salah input, user tidak
bisa mengubahnya meskipun sudah diketahui salah.

### Solution

#### 1. View Override (`views/stock_picking_views.xml`)
- Menambahkan view baru `view_picking_form_carrier_tracking_ref_editable` (priority 99)
- Menghapus atribut `readonly` pada field `carrier_tracking_ref` sehingga field tetap bisa
  diedit meskipun DO sudah `done` / `cancel`.

#### 2. Write Method Update (`models/stock_picking.py`)
- Method `write()` diupdate untuk mendeteksi perubahan pada `carrier_tracking_ref`.
- Saat resi berubah, otomatis:
  - `delivery_tracking_status` di-reset ke `draft`
  - `delivery_tracking_status_desc` di-clear
  - `delivery_tracking_error` di-clear
  - `delivery_tracking_last_check` di-reset
  - `tracking_notification_sent` di-reset (supaya notifikasi Telegram baru terkirim)

### Behavior After Change
- User bisa mengedit No. Resi kapan saja, termasuk setelah DO validated.
- Saat resi diubah, cron akan otomatis mulai tracking ulang dengan resi baru.
- Notifikasi Telegram akan terkirim ulang ke salesperson dengan informasi resi baru.
- Jika status sebelumnya `delivered` / `exception` (final), cron tidak akan otomatis poll.
  User perlu klik tombol "Cek Status Pengiriman" secara manual.

### Files Changed
| File | Change |
|------|--------|
| `views/stock_picking_views.xml` | Tambah view override readonly `carrier_tracking_ref` |
| `models/stock_picking.py` | Update `write()` — reset tracking saat resi berubah |
