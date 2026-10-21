# stock_picking_delivery_tracking
Integrasi pengiriman dengan API Biteship

## Fitur

- Polling status pengiriman Delivery Order via API Biteship
  (JNE, J&T, Wahana, Pos Indonesia, Lion Parcel).
- Dashboard kanban status tracking.
- Notifikasi Telegram satu kali ke Salesperson (penanggung jawab SO) saat
  nomor resi (`carrier_tracking_ref`) pertama kali diinput pada DO.
- **Portal tracking customer** — halaman timeline/stepper status pengiriman,
  bisa diakses lewat login portal (`/my/orders`) atau link publik tanpa login
  (`/tracking/<token>`).

## Portal Tracking Customer

### 1. Akses Login (Portal)

Customer dengan akun portal membuka **My Orders** → kolom **Tracking** untuk
melihat timeline pengiriman order miliknya. Akses dibatasi ke partner pemilik
order (customer lain tidak bisa membuka meskipun menebak ID).

### 2. Akses Publik (Link Token)

Untuk customer tanpa akun portal, sales/CS bisa mengirim link tracking:

1. Buka form Delivery Order (outgoing).
2. Copy field **"Link Tracking Publik"** (tombol copy tersedia langsung di
   form, widget `CopyClipboardURL`).
3. Kirim link via WhatsApp/email.

Format URL: `https://<base-url>/tracking/<token>`. Token di-generate otomatis
saat DO dibuat dan diganti otomatis saat nomor resi atau kurir diubah.

### 3. Timeline

6 milestone: Order Dibuat → Diproses → Dikirim → Dalam Perjalanan → Diantar →
Selesai (+ banner untuk status kendala/retur atau belum diketahui). Responsive
horizontal di desktop, vertikal di mobile. Riwayat status detail bisa
di-expand.

> Keamanan: halaman publik hanya menampilkan data tracking (resi, kurir, ETA,
> timeline, riwayat). Tidak ada harga, alamat lengkap, atau data pelanggan lain.

## Setup Notifikasi Telegram ke Sales

### 1. Telegram Bot Token

Token TIDAK pernah di-hardcode di source code. Isi manual di
**Settings > Inventory/Delivery > Delivery Tracking > Telegram Bot Token**
setelah module dideploy.

Cara membuat bot: chat `@BotFather` di Telegram, buat bot baru, lalu salin
token ke Settings di atas.

> Keamanan: jangan tempel token di chat/issue/prompt. Kalau token sempat
> bocor, regenerate via BotFather.

### 2. Chat ID Salesperson

- Salesperson chat `/start` ke bot.
- Admin ambil `chat_id` dari `getUpdates`, lalu isi field
  **Telegram Chat ID** pada form user
  (tab "Telegram (Delivery Tracking)").
- Jika field sudah ada dari module lain (`purchase_request_telegram_notify`),
  field yang sama otomatis ter-pakai (shared).

### 3. Toggle

**Settings > Delivery Tracking > "Notify Salesperson on Tracking Reference"**
(default aktif). Matikan untuk menghentikan semua notifikasi tanpa mencabut
token.

### 4. Kirim Ulang Manual

Button **"Kirim Ulang Notifikasi"** pada form DO (muncul jika nomor resi
sudah terisi). Dipakai kalau salesperson belum punya `telegram_chat_id` saat
notifikasi pertama gagal, atau butuh kirim ulang.

## Catatan Teknis

- Anti double-send: field `tracking_notification_sent` di `stock.picking`.
  Set `True` hanya setelah kirim berhasil. Migrasi upgrade menandai DO
  existing yang sudah punya resi sebagai "sudah dikirim" supaya tidak ada
  notifikasi retroaktif.
- Kegagalan kirim Telegram tidak pernah mem-blok proses `write()` DO.

## Known Limitations

- **Notifikasi hanya ter-trigger lewat `write()`** saat `carrier_tracking_ref`
  berubah (dari kosong menjadi terisi) atau saat `carrier_id` diubah.
  Proses `create()` dengan
  `carrier_tracking_ref` sudah terisi langsung (misal bulk import, migrasi
  data dari sistem lain, atau pembuatan DO lewat API) TIDAK memicu notifikasi
  otomatis.
- Untuk DO hasil bulk import / migrasi dengan resi yang butuh notifikasi,
  trigger manual lewat button **"Kirim Ulang Notifikasi"** pada form DO, atau
  lewat script terpisah (panggil `picking._notify_sales_telegram()` per DO).

