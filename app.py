from machine import Pin, ADC, I2C
import time
import json
import gc
import network
import bluetooth
import ssd1306

try:
    import urequests as requests
except ImportError:
    import requests

# ================= SETTINGS =================
DEVICE_ID = "ESP32_01"           # must match the device id used in the app / Firebase
FIRMWARE = "1.0"
BLE_NAME = "PlantGuardian"

# Water relay (pump)
RELAY_PIN = 26
ACTIVE_LOW = True                # True: relay ON when pin LOW

# Sensors (GPIO36 = VP and GPIO34: input-only, analog, no pull-ups)
WATER_PIN = 36                   # water level sensor (reported only)
SOIL_PIN = 34                    # soil moisture sensor
SOIL_DRY_RAW = 3200              # raw reading in air   (calibrate)
SOIL_WET_RAW = 1400              # raw reading in water (calibrate)

# OLED 0.93" SSD1306 128x64 I2C
OLED_SDA = 21
OLED_SCL = 22

# Firebase (same project as the app)
DB_URL = "https://smart-plant-guardian-default-rtdb.asia-south1.firebasedatabase.app"
API_KEY = "AIzaSyDWdGtUvjhiaeed_YPdFOi2Ja_Gcq0Ih4o"
DEVICE_EMAIL = "esp32@example.com"     # <-- a Firebase Auth user created for the ESP32
DEVICE_PASSWORD = "CHANGE_ME"          # <-- its password
FIREBASE_INTERVAL = 5            # seconds between cloud sync cycles
HISTORY_INTERVAL = 300           # seconds between history points

EMOJI_SECONDS = 3
EMOJI_REPEAT = 30
# ============================================

# ---------- State (defaults; app settings overwrite them) ----------
power = True                     # device on/off (soft power)
pump_cmd = False                 # MANUAL pump command
mode = "AUTO"                    # "AUTO" | "MANUAL"
soil_dry = 30                    # % below which AUTO starts the pump
soil_target = 65                 # % at which AUTO stops the pump
max_run_min = 15                 # max continuous pump run time
pump_on = False
run_start = 0
block_until = time.ticks_ms()
sync_pending = False             # push local state to Firebase commands

water_raw = 0
soil_raw = 0
soil_pct = 0

# ---------- Relay (level set before output: no boot click) ----------
OFF_VAL = 1 if ACTIVE_LOW else 0
ON_VAL = 1 - OFF_VAL
relay = Pin(RELAY_PIN, Pin.OUT, value=OFF_VAL)


def safe_off():
    """Call before machine.reset()."""
    relay.value(OFF_VAL)


# ---------- Sensors ----------
def make_adc(pin):
    a = ADC(Pin(pin))
    a.atten(ADC.ATTN_11DB)
    try:
        a.width(ADC.WIDTH_12BIT)
    except Exception:
        pass
    return a


water_adc = make_adc(WATER_PIN)
soil_adc = make_adc(SOIL_PIN)


def read_avg(a, n=16):
    t = 0
    for _ in range(n):
        t += a.read()
        time.sleep_ms(2)
    return t // n


# ---------- OLED ----------
i2c = I2C(0, scl=Pin(OLED_SCL), sda=Pin(OLED_SDA), freq=400000)
oled = ssd1306.SSD1306_I2C(128, 64, i2c)


def oled_power(on):
    try:
        if on:
            oled.poweron()
        else:
            oled.fill(0)
            oled.show()
            oled.poweroff()
    except Exception:
        pass


# ---------- Commands (from BLE or Firebase) ----------
def to_bool(v):
    return v is True or v in (1, "1", "true", "True", "on", "ON")


def apply_command(key, val, from_ble=False):
    global power, pump_cmd, mode, sync_pending
    changed = False
    if key == "power":
        v = to_bool(val)
        if v != power:
            power = v
            changed = True
            oled_power(v)
            if not v:
                pump_cmd = False
                sync_pending = True      # write pump=false back to the cloud
    elif key == "mode":
        m = "AUTO" if str(val).upper() == "AUTO" else "MANUAL"
        if m != mode:
            mode = m
            changed = True
    elif key == "pump":
        if power:
            v = to_bool(val)
            if v != pump_cmd:
                pump_cmd = v
                changed = True
    if changed and from_ble:
        sync_pending = True


def apply_pump():
    global pump_on, run_start, pump_cmd, sync_pending, block_until
    now = time.ticks_ms()
    want = False
    if power:
        if mode == "AUTO":
            if pump_on:
                want = soil_pct < soil_target
            else:
                want = soil_pct < soil_dry and time.ticks_diff(now, block_until) >= 0
        else:
            want = pump_cmd
    # safety: max continuous run time
    if want and pump_on and time.ticks_diff(now, run_start) > max_run_min * 60000:
        want = False
        block_until = time.ticks_add(now, 300000)   # 5 min cool-down in AUTO
        if mode == "MANUAL":
            pump_cmd = False
            sync_pending = True
    if want and not pump_on:
        run_start = now
    pump_on = want
    relay.value(ON_VAL if want else OFF_VAL)


def run_seconds():
    if not pump_on:
        return 0
    return time.ticks_diff(time.ticks_ms(), run_start) // 1000


def control():
    global water_raw, soil_raw, soil_pct
    water_raw = read_avg(water_adc)
    soil_raw = read_avg(soil_adc)
    p = (SOIL_DRY_RAW - soil_raw) * 100 // (SOIL_DRY_RAW - SOIL_WET_RAW)
    soil_pct = max(0, min(100, p))
    apply_pump()


# ---------- Bluetooth LE (offline link to the phone app) ----------
_SVC_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
_TX_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"   # notify: sensor data
_RX_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"   # write : commands

ble = bluetooth.BLE()
ble_conns = set()
ble_rx = []
h_tx = h_rx = None
last_notify = 0


def _ble_irq(event, data):
    if event == 1:                       # central connected
        ble_conns.add(data[0])
    elif event == 2:                     # central disconnected
        ble_conns.discard(data[0])
        ble_advertise()
    elif event == 3:                     # write
        if data[1] == h_rx:
            try:
                ble_rx.append(ble.gatts_read(h_rx).decode().strip())
            except Exception:
                pass


def ble_advertise():
    try:
        uuid_le = bytes(reversed(bytes.fromhex(_SVC_UUID.replace("-", ""))))
        adv = b"\x02\x01\x06" + b"\x11\x07" + uuid_le
        name = BLE_NAME.encode()
        resp = bytes((len(name) + 1, 0x09)) + name
        ble.gap_advertise(100000, adv_data=adv, resp_data=resp)
    except Exception as e:
        print("BLE adv error:", e)


def ble_start():
    global h_tx, h_rx
    try:
        ble.active(True)
        ble.config(gap_name=BLE_NAME)
        ble.irq(_ble_irq)
        tx = (bluetooth.UUID(_TX_UUID), bluetooth.FLAG_NOTIFY | bluetooth.FLAG_READ)
        rx = (bluetooth.UUID(_RX_UUID), bluetooth.FLAG_WRITE | bluetooth.FLAG_WRITE_NO_RESPONSE)
        ((h_tx, h_rx),) = ble.gatts_register_services(((bluetooth.UUID(_SVC_UUID), (tx, rx)),))
        ble_advertise()
        print("BLE advertising as", BLE_NAME)
    except Exception as e:
        print("BLE start error:", e)


def ble_connected():
    return len(ble_conns) > 0


def ble_notify():
    # "soil%,water,pump,power,mode,runSec"  e.g. 45,1800,1,1,A,120  (<20 bytes)
    msg = "%d,%d,%d,%d,%s,%d" % (
        soil_pct, water_raw, 1 if pump_on else 0, 1 if power else 0,
        mode[0], min(run_seconds(), 9999))
    try:
        ble.gatts_write(h_tx, msg)
        for c in list(ble_conns):
            ble.gatts_notify(c, h_tx, msg)
    except Exception as e:
        print("BLE notify error:", e)


def ble_handle_commands():
    while ble_rx:
        msg = ble_rx.pop(0)
        k, _, v = msg.partition("=")        # e.g. "pump=1", "power=0", "mode=AUTO"
        apply_command(k.strip(), v.strip(), from_ble=True)


# ---------- Firebase (used only when the phone is NOT connected by BLE) ----------
token = None
token_exp = 0
settings_count = 0
last_history = time.ticks_ms()


def wifi_ok():
    return network.WLAN(network.STA_IF).isconnected()


def fb_login():
    global token, token_exp
    r = None
    try:
        gc.collect()
        r = requests.post(
            "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=" + API_KEY,
            data=json.dumps({"email": DEVICE_EMAIL, "password": DEVICE_PASSWORD,
                             "returnSecureToken": True}),
            headers={"Content-Type": "application/json"})
        d = r.json()
        if "idToken" in d:
            token = d["idToken"]
            secs = int(d.get("expiresIn", "3600")) - 300
            token_exp = time.ticks_add(time.ticks_ms(), secs * 1000)
            return True
        print("Firebase login failed:", d.get("error", {}).get("message"))
    except Exception as e:
        print("Firebase login error:", e)
    finally:
        if r:
            try:
                r.close()
            except Exception:
                pass
    return False


def fb(method, path, payload=None):
    """Returns (ok, json_or_None). path: "", "/commands", "/settings", "/history"."""
    global token
    if not wifi_ok():
        return False, None
    if token is None or time.ticks_diff(time.ticks_ms(), token_exp) >= 0:
        if not fb_login():
            return False, None
    gc.collect()
    url = "%s/devices/%s%s.json?auth=%s" % (DB_URL, DEVICE_ID, path, token)
    r = None
    try:
        body = json.dumps(payload) if payload is not None else None
        if method == "GET":
            r = requests.get(url)
        elif method == "PATCH":
            r = requests.patch(url, data=body)
        else:
            r = requests.post(url, data=body)
        if r.status_code == 200:
            return True, (r.json() if method == "GET" else None)
        print("Firebase", method, path, "HTTP", r.status_code)
        if r.status_code == 401:
            token = None
    except Exception as e:
        print("Firebase error:", e)
    finally:
        if r:
            try:
                r.close()
            except Exception:
                pass
    return False, None


def firebase_cycle():
    global sync_pending, settings_count, last_history
    global soil_dry, soil_target, max_run_min

    # 1) commands: push local state first if BLE changed it, else read the app's commands
    if sync_pending:
        ok, _ = fb("PATCH", "/commands", {"power": power, "pump": pump_cmd, "mode": mode})
        if ok:
            sync_pending = False
    else:
        ok, d = fb("GET", "/commands")
        if ok and isinstance(d, dict):
            for k in ("power", "mode", "pump"):
                if k in d:
                    apply_command(k, d[k])

    # 2) settings (thresholds) every ~1 min
    settings_count += 1
    if settings_count >= 12:
        settings_count = 0
        ok, s = fb("GET", "/settings")
        if ok and isinstance(s, dict):
            soil_dry = int(s.get("soilDryPercent", soil_dry))
            soil_target = int(s.get("soilTargetPercent", soil_target))
            max_run_min = int(s.get("maxPumpRunTimeMinutes", max_run_min))

    # 3) sensors + status (+ meta) in one multi-path update
    wlan = network.WLAN(network.STA_IF)
    fb("PATCH", "", {
        "sensors/soilZone1": soil_pct,
        "sensors/waterRaw": water_raw,
        "sensors/lastSeenEpochMs": {".sv": "timestamp"},
        "status/pumpOn": pump_on,
        "status/mode": mode,
        "status/activeRunSeconds": run_seconds(),
        "meta/firmwareVersion": FIRMWARE,
        "meta/ipAddress": wlan.ifconfig()[0],
    })

    # 4) history point every HISTORY_INTERVAL
    if time.ticks_diff(time.ticks_ms(), last_history) >= HISTORY_INTERVAL * 1000:
        last_history = time.ticks_ms()
        fb("POST", "/history", {
            "timestampMs": {".sv": "timestamp"},
            "soilAvg": soil_pct,
            "zone1": soil_pct,
        })


# ---------- Display ----------
def draw_emoji(blink, happy):
    oled.fill(0)
    oled.rect(24, 0, 80, 64, 1)
    if blink:
        oled.hline(42, 22, 14, 1)
        oled.hline(72, 22, 14, 1)
    else:
        oled.fill_rect(42, 16, 14, 14, 1)
        oled.fill_rect(72, 16, 14, 14, 1)
    if happy:
        oled.hline(48, 48, 32, 1)
        oled.line(48, 48, 42, 42, 1)
        oled.line(80, 48, 86, 42, 1)
    else:
        oled.hline(48, 44, 32, 1)
        oled.line(48, 44, 42, 50, 1)
        oled.line(80, 44, 86, 50, 1)
    oled.show()


def show_emoji(seconds=EMOJI_SECONDS):
    start = time.ticks_ms()
    n = 0
    while time.ticks_diff(time.ticks_ms(), start) < seconds * 1000 and power:
        draw_emoji(n % 3 == 2, soil_pct >= soil_dry)
        n += 1
        service()
        time.sleep_ms(250)


def show_status():
    oled.fill(0)
    oled.text("Soil:%3d%% %s" % (soil_pct, mode[:4]), 0, 0)
    oled.text("Water:%4d" % water_raw, 0, 14)
    oled.text("Pump: " + ("ON" if pump_on else "OFF"), 0, 28)
    link = "BLE" if ble_connected() else ("WiFi" if wifi_ok() else "none")
    oled.text("Link: " + link, 0, 42)
    oled.show()


# ---------- Service tick ----------
def service():
    global last_notify
    ble_handle_commands()
    control()
    if ble_connected() and time.ticks_diff(time.ticks_ms(), last_notify) >= 1000:
        last_notify = time.ticks_ms()
        ble_notify()


# ---------- Main ----------
ble_start()
control()
show_emoji()
last_emoji = time.ticks_ms()
last_fb = time.ticks_ms() - FIREBASE_INTERVAL * 1000

while True:
    try:
        service()
        now = time.ticks_ms()
        if power:
            show_status()
            if time.ticks_diff(now, last_emoji) >= EMOJI_REPEAT * 1000:
                show_emoji()
                last_emoji = time.ticks_ms()
        # phone in Bluetooth range -> offline link; otherwise cloud
        if not ble_connected() and time.ticks_diff(now, last_fb) >= FIREBASE_INTERVAL * 1000:
            last_fb = now
            firebase_cycle()
        time.sleep_ms(200)
    except Exception as e:
        print("Loop error:", e)
        safe_off()
        time.sleep(1)
