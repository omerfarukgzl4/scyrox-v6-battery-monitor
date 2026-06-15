r"""
Scyrox V6 Tray Battery Monitor
==============================

Scyrox V6 kablosuz mouse'un pil yüzdesini sistem tepsisinden (tray)
sorgulamak için yazılmış, tamamen pasif çalışan bir yardımcı uygulama.
Resmi Scyrox web arayüzüne her seferinde girmek zorunda kalmamak için
yapıldı.

Davranış
--------
* Uygulama açıldığında tepside özel mouse ikonu (Scyrox_V6_blue.ico) durur.
  Bu halde hiçbir arka plan etkinliği yoktur; dongle'a paket gönderilmez,
  zamanlayıcı çalışmaz. Mouse'un gerçek zamanlı radyo performansına etkisi
  yoktur.
* Kullanıcı ikona sol tıkladığında dongle'a tek bir HID sorgusu atılır.
  Cevap gelince ikon 10 saniye boyunca pil yüzdesini ve duruma göre renkli
  bir göstergeyi yansıtır; sonra tekrar mouse ikonuna döner.
* İkona sağ tıklayınca son ölçülen pil %, voltaj, ölçüm saati ve
  çıkış seçeneği içeren küçük bir menü açılır. Menü yalnızca okuma
  amaçlıdır, orada yeni sorgu atılmaz.

HID Protokolü (Scyrox / Compx)
------------------------------
Hedef cihaz: VID 0x3554, PID 0xF5F7, usage_page 0xFF02 koleksiyonu.

Sorgu (17 bayt, report id dahil)::

    [0] 0x08   HID report ID
    [1] 0x04   Komut: "battery"
    [2] 0x01   Bayrak: READ (0x00 olursa dongle yazma/ACK'e geçer)
    [3] 0x00   Reserved
    [4] 0x00   Adres
    [5] 0x00   Veri uzunluğu (biz veri göndermiyoruz -> 0)
    [6..15]    Dolgu (0x00)
    [16]       Checksum = (0x55 - sum(bytes[0..15])) & 0xFF

Cevap (17 bayt)::

    [0]  0x08
    [1]  0x04
    [2]  0x00   Başarılı data cevabı (0x01 gelirse ACK, veri yok)
    [3]  0x00
    [4]  0x00
    [5]  0x02   Veri uzunluğu
    [6]  pct   Pil yüzdesi (0..100)
    [7]  --    Kullanılmıyor (dongle üzerinden şarj tespiti çalışmıyor,
                mouse şarj olurken USB ile doğrudan bağlı; dongle yolu
                bu durumu göremiyor)
    [8]  mv_hi \ Voltaj mV big-endian (örn. 0x0F 0x4D = 3917 mV)
    [9]  mv_lo /
    [10..15]  Dolgu
    [16]       Checksum

Notlar
------
Dongle'dan cevap olarak gelen pil yüzdesi %5'in katları şeklinde olmaktadır.
Web arayüzü muhtemelen voltaj bilgisinden bir hesaplama yaparak pil yüzdesini
daha hassas göstermektedir. Fakat bu hesaplama çok yüksek doğruluğa sahip değildir.
Bu yüzden web arayüzündeki pil sık sık kontrol edilirse özellikle fare aç kapa yapıldıktan sonra %3-5
artış/azalışlar görülebilir. Bu uygulamada ise böyle bir durum olmamaktadır, 
sadece 5'in katları şeklinde bir pil yüzdesi gösterilmektedir.


"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import warnings

import hid
from PIL import Image, ImageDraw, ImageFont
import pystray

# PIL IcoImagePlugin hatalı okunan (boyutu yanlış) .ico dosyaları için uyarı veriyor, bunu gizle
warnings.filterwarnings("ignore", category=UserWarning, module="PIL.IcoImagePlugin")


# =========================================================================
# Sabitler
# =========================================================================

VENDOR_ID = 0x3554
PRODUCT_ID = 0xF5F7

# Dongle'ın konfigürasyon kanalı. Mouse'un klavye/fare sahte arayüzleri
# değil, üreticiye özel (vendor-specific) koleksiyonu.
TARGET_USAGE_PAGE = 0xFF02
# Sorgu cevabını en fazla bu kadar bekleriz. Tipik 10-50 ms içinde döner.
READ_TIMEOUT_MS = 600

# Sol tıktan sonra pil göstergesi tepside ne kadar kalsın.
SHOW_BATTERY_SECONDS = 10.0

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IDLE_ICON_PATH = os.path.join(SCRIPT_DIR, "Scyrox_V6_blue.ico")


# =========================================================================
# Protokol yardımcıları
# =========================================================================

def compx_checksum(body16) -> int:
    """Scyrox/Compx checksum: (0x55 - toplam) & 0xFF.

    Paketin ilk 16 baytı toplanır, 0x55 sabitinden çıkarılır ve sonuç
    son bayt olarak eklenir. Bu formül, site-dongle trafiğinin sniff
    edilmesiyle doğrulanmıştır.
    """
    return (0x55 - sum(body16)) & 0xFF


def build_battery_query() -> bytes:
    """Pil yüzdesi sorgu paketini üret."""
    body = [0x08, 0x04, 0x01, 0x00, 0x00, 0x00] + [0x00] * 10
    return bytes(body + [compx_checksum(body)])


BATTERY_QUERY = build_battery_query()


def parse_battery(report: bytes) -> Optional[tuple[int, int]]:
    """Gelen bir HID raporunu pil cevabı olarak yorumla.

    Döner: (yüzde, voltaj_mV) veya None (format uymazsa).
    """
    if len(report) < 10:
        return None
    if report[0] != 0x08 or report[1] != 0x04:
        return None
    # byte 2 = 0x00 -> veri cevabı, 0x01 -> sadece ACK (data yok)
    if report[2] != 0x00:
        return None

    pct = report[6]
    if not 0 <= pct <= 100:
        return None

    mv = (report[8] << 8) | report[9]  # big-endian
    return pct, mv


# =========================================================================
# HID katmanı
# =========================================================================

def find_target_path() -> Optional[bytes]:
    """Dongle'ın konfigürasyon koleksiyonunun sistem yolunu bul."""
    for d in hid.enumerate(VENDOR_ID, PRODUCT_ID):
        if d.get("usage_page") == TARGET_USAGE_PAGE:
            return d["path"]
    return None


@dataclass
class BatteryState:
    """Son başarılı (veya başarısız) ölçümün bilgisi."""

    percent: Optional[int] = None
    voltage_mv: Optional[int] = None
    last_update: Optional[datetime] = None
    last_error: Optional[str] = None


def _invalidate(state: BatteryState, error: str) -> None:
    """Ölçüm başarısızsa eski değerleri tut(ma); kullanıcıya bayat veri
    göstermeyelim (örn. dongle çıkarıldıktan sonra hâlâ %50 yazmasın)."""
    state.percent = None
    state.voltage_mv = None
    state.last_update = None
    state.last_error = error


def query_battery_once(state: BatteryState) -> bool:
    """Dongle'a bir sorgu atar, state'i yerinde günceller.

    Başarılı olduğunda True döner ve state taze değerlerle doldurulur.
    Aksi halde False döner, state temizlenir ve `last_error` doldurulur.
    Her çağrıda HID handle açılıp kapandığı için çağrılar arasında
    hiçbir kaynak tutulmaz.
    """
    path = find_target_path()
    if path is None:
        _invalidate(state, "Dongle bulunamadı (takılı mı?)")
        return False

    h = hid.device()
    try:
        h.open_path(path)
    except Exception as e:
        _invalidate(state, f"Dongle açılamadı: {e}")
        return False

    try:
        h.set_nonblocking(True)

        # Tamponda bekleyen eski paketleri at (50 ms drenaj). Aksi halde
        # biraz önceki cevabı yeniymiş gibi okuyabiliriz.
        deadline = time.time() + 0.05
        while time.time() < deadline:
            try:
                if not h.read(64, timeout_ms=10):
                    break
            except Exception:
                break

        # Sorguyu yolla
        try:
            h.write(BATTERY_QUERY)
        except Exception as e:
            _invalidate(state, f"Sorgu yazılamadı: {e}")
            return False

        # Cevabı bekle (READ_TIMEOUT_MS içinde pil formatında paket gelene
        # kadar diğer gelen raporları atla)
        deadline = time.time() + READ_TIMEOUT_MS / 1000.0
        while time.time() < deadline:
            try:
                data = h.read(64, timeout_ms=80)
            except Exception as e:
                _invalidate(state, f"Okuma hatası: {e}")
                return False
            if not data:
                continue

            parsed = parse_battery(bytes(data))
            if parsed is not None:
                pct, mv = parsed
                state.percent = pct
                state.voltage_mv = mv
                state.last_update = datetime.now()
                state.last_error = None
                return True

        _invalidate(state, "Cevap gelmedi (zaman aşımı)")
        return False

    finally:
        try:
            h.close()
        except Exception:
            pass


# =========================================================================
# Görsel üretimi
# =========================================================================

# Pil yüzdesi -> ikon rengi eşlemesi.
COLOR_HIGH       = (30, 160, 60, 255)    # koyu yeşil  (%50+)
COLOR_MID        = (130, 220, 90, 255)   # açık yeşil  (%30-50)
COLOR_LOW        = (255, 150, 30, 255)   # turuncu     (%20-30)
COLOR_CRITICAL   = (230, 70, 70, 255)    # kırmızı     (<%20)
COLOR_UNKNOWN    = (200, 80, 80, 255)    # ölçüm alınamadı


def _pick_color(state: BatteryState) -> tuple[int, int, int, int]:
    pct = state.percent
    if pct is None:
        return COLOR_UNKNOWN
    if pct >= 50:
        return COLOR_HIGH
    if pct >= 30:
        return COLOR_MID
    if pct >= 20:
        return COLOR_LOW
    return COLOR_CRITICAL


def load_idle_icon() -> Image.Image:
    """Boşta kalınca gösterilecek mouse ikonu. Dosya yoksa placeholder."""
    try:
        return Image.open(IDLE_ICON_PATH)
    except Exception:
        img = Image.new("RGBA", (64, 64), (50, 100, 200, 255))
        return img


def make_battery_icon(state: BatteryState) -> Image.Image:
    """Pil yüzdesini içeren, pikselleri keskinleştirilmiş kare ikonu üret."""
    # Tam olarak piksellere karşılık gelmesi ve kenar yumuşatmasının (anti-aliasing)
    # engellenmesi için 16x16 lık ızgarada çizim yapılıp NEAREST ile büyütülür.
    base_size = 16
    img = Image.new("RGBA", (base_size, base_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    text = f"{state.percent}" if state.percent is not None else "!"
    border = _pick_color(state)

    # Arka plan + tek piksellik keskin renkli çerçeve
    draw.rectangle((0, 0, base_size - 1, base_size - 1),
                   fill=(20, 20, 24, 255), outline=border, width=1)

    # Yumuşatmasız (aliased) varsayılan bitmap font
    font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = (base_size - tw) // 2 - bbox[0]
    y = (base_size - th) // 2 - bbox[1]
    draw.text((x, y), text, fill=(255, 255, 255, 255), font=font)

    # İkonu keskin 4x4 piksellik bloklara dönüştürmek için 64x64'e büyüt.
    # Böylece Windows tray ikonu küçültse bile pikseller okunabilir kalır.
    resample_filter = getattr(Image.Resampling, "NEAREST", 0) if hasattr(Image, "Resampling") else getattr(Image, "NEAREST", 0)
    return img.resize((64, 64), resample=resample_filter)


# =========================================================================
# Tooltip ve menü metinleri
# =========================================================================

def tooltip_idle(state: BatteryState) -> str:
    """Mouse ikonundayken (pasif mod) fare üzerine gelince görünen yazı."""
    if state.percent is None:
        return "Scyrox V6  (Pil bilgisi için sol tıklayın)"
    ts = state.last_update.strftime("%H:%M:%S") if state.last_update else "-"
    return f"Scyrox V6  Son ölçüm: %{state.percent}  {ts}"


def tooltip_active(state: BatteryState) -> str:
    """Pil ikonuna geçici olarak geçildiğinde görünen yazı."""
    if state.last_error and state.percent is None:
        return f"Scyrox: {state.last_error}"
    if state.percent is None:
        return "Scyrox: ölçüm başarısız"

    parts = [f"Pil: %{state.percent}"]
    if state.voltage_mv:
        parts.append(f"{state.voltage_mv/1000:.2f}V")
    return "  ".join(parts)


# =========================================================================
# Tray uygulaması
# =========================================================================

class App:
    """Tray simgesini ve durum geçişlerini yöneten sınıf."""

    def __init__(self) -> None:
        self.state = BatteryState()
        self.idle_icon = load_idle_icon()
        self.revert_timer: Optional[threading.Timer] = None
        self.lock = threading.Lock()
        self.icon: Optional[pystray.Icon] = None

        # Meşgul kilidi: tıklamadan pil ikonu tekrar mouse ikonuna dönene
        # kadar True kalır. Bu süre zarfındaki ek sol tıklar yok sayılır;
        # böylece paralel worker / HID handle çakışması oluşmaz.
        self.busy = False

    # ---- ikon değiştirme ------------------------------------------------

    def show_idle(self) -> None:
        """Pasif mouse ikonuna geri dön ve meşgul kilidini serbest bırak."""
        if self.icon is None:
            return
        self.icon.icon = self.idle_icon
        self.icon.title = tooltip_idle(self.state)
        with self.lock:
            self.busy = False

    def show_battery_temporary(self) -> None:
        """Pil yüzdesi ikonunu göster ve SHOW_BATTERY_SECONDS sonra geri dön."""
        if self.icon is None:
            return
        self.icon.icon = make_battery_icon(self.state)
        self.icon.title = tooltip_active(self.state)

        with self.lock:
            # Önceki geri-dönüş zamanlayıcısını iptal edip yenisini kur.
            # Böylece kullanıcı 10 sn bitmeden tekrar tıkladığında süre
            # sıfırdan başlar.
            if self.revert_timer is not None:
                self.revert_timer.cancel()
            timer = threading.Timer(SHOW_BATTERY_SECONDS, self.show_idle)
            timer.daemon = True
            self.revert_timer = timer
            timer.start()

    # ---- olay işleyicileri ---------------------------------------------

    def on_left_click(self, _icon, _item) -> None:
        """Tray'deki sol tıkla tetiklenen aksiyon.

        Sorgu + 10 sn gösterim süresince `busy` bayrağı True olduğu için
        ek tıklar yok sayılır. Böylece hem paralel HID çakışması engellenir,
        hem de süre sayacının sıfırlanıp durması gibi tuhaflıklar önlenir.

        Sorguyu ayrı bir thread'de çalıştırırız; pystray olay döngüsü
        HID okuması sırasında bloklanmamalı.
        """
        with self.lock:
            if self.busy:
                return
            self.busy = True

        def worker() -> None:
            query_battery_once(self.state)
            self.show_battery_temporary()
            # Menüdeki dinamik metinleri (pil %, voltaj, saat) yenile.
            try:
                if self.icon is not None:
                    self.icon.update_menu()
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def on_quit(self, icon, _item) -> None:
        """Uygulamayı kapat."""
        with self.lock:
            if self.revert_timer is not None:
                self.revert_timer.cancel()
        icon.stop()

    # ---- menü ----------------------------------------------------------

    def build_menu(self) -> pystray.Menu:
        """Sağ tık menüsü. Etiketler fonksiyon olarak tanımlı;
        pystray her açılışta bunları tekrar çağırdığından son ölçüm
        değerleri otomatik yansır."""
        s = self.state

        def percent_label(_item):
            if s.last_error and s.percent is None:
                return s.last_error
            if s.percent is None:
                return "Henüz ölçüm yok"
            return f"Pil: %{s.percent}"

        def voltage_label(_item):
            if s.voltage_mv:
                return f"Voltaj: {s.voltage_mv/1000:.2f} V"
            return "Voltaj: -"

        def time_label(_item):
            if s.last_update:
                return f"Son ölçüm: {s.last_update.strftime('%H:%M:%S')}"
            return "Son ölçüm: -"

        return pystray.Menu(
            # Görünmez "varsayılan" eylem: pystray'de sol-tık == default=True
            # olan item'ın tetiklenmesi. visible=False ile menüde listelenmez.
            pystray.MenuItem("Ölç", self.on_left_click,
                             default=True, visible=False),

            pystray.MenuItem(percent_label, None, enabled=False),
            pystray.MenuItem(voltage_label, None, enabled=False),
            pystray.MenuItem(time_label,    None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Çıkış", self.on_quit),
        )

    # ---- giriş noktası --------------------------------------------------

    def run(self) -> int:
        self.icon = pystray.Icon(
            "scyrox-battery",
            icon=self.idle_icon,
            title=tooltip_idle(self.state),
            menu=self.build_menu(),
        )
        self.icon.run()  # Windows olay döngüsü; icon.stop() çağrılana kadar bloklanır
        return 0


def main() -> int:
    return App().run()


if __name__ == "__main__":
    sys.exit(main())
