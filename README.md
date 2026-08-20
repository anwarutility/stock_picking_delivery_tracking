# stock_picking_delivery_tracking
Integrasi pengiriman dengan API Biteship

## Fitur

- Polling status pengiriman Delivery Order via API Biteship
  (JNE, J&T, Wahana, Pos Indonesia, Lion Parcel).
- Dashboard kanban status tracking.
- Notifikasi Telegram satu kali ke Salesperson (penanggung jawab SO) saat
  nomor resi (`carrier_tracking_ref`) pertama kali diinput pada DO.

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
  berubah dari kosong menjadi terisi. Proses `create()` dengan
  `carrier_tracking_ref` sudah terisi langsung (misal bulk import, migrasi
  data dari sistem lain, atau pembuatan DO lewat API) TIDAK memicu notifikasi
  otomatis.
- Untuk DO hasil bulk import / migrasi dengan resi yang butuh notifikasi,
  trigger manual lewat button **"Kirim Ulang Notifikasi"** pada form DO, atau
  lewat script terpisah (panggil `picking._notify_sales_telegram()` per DO).
