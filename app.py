from machine import Pin, SPI, ADC, PWM
import time
import dht
import framebuf

# =====================================================
#  PIN SETTINGS  (change here if your board differs)
# =====================================================
OLED_SCK, OLED_MOSI = 18, 23
OLED_RES, OLED_DC, OLED_CS = 2, 19, 21

DHT_PIN = 15
SOIL1_PIN, SOIL2_PIN, SOIL3_PIN = 32, 33, 35
LDR_PIN = 34
VOLT_PIN = 39
RAIN_DO_PIN = 22          # rain module DO (digital) pin

PUMP_PIN = 27
HUMIDIFIER_PIN = 14
AUDIO_PIN = 25
LED_RED_PIN = 26          # was GPIO12 (boot-strapping pin)
LED_BLUE_PIN = 13
LED_GREEN_PIN = 5
LED_YELLOW_PIN = 16

# =====================================================
#  THRESHOLDS  (raw ADC: HIGH = dry soil, LOW = wet)
# =====================================================
SOIL_DRY_ON = 2600        # soil counts as dry above this
SOIL_DRY_OFF = 2400       # soil counts as wet again below this
HUM_LOW_ON = 58.0         # humidifier ON below this
HUM_LOW_OFF = 62.0        # humidifier OFF above this

WIDTH = 128
HEIGHT = 64

spi = SPI(1, baudrate=10000000, polarity=0, phase=0,
          sck=Pin(OLED_SCK), mosi=Pin(OLED_MOSI))
res_pin = Pin(OLED_RES, Pin.OUT)
dc_pin = Pin(OLED_DC, Pin.OUT)
cs_pin = Pin(OLED_CS, Pin.OUT)


# --- Built-in Mini SSD1306 SPI Driver Class ---
class SSD1306_SPI(framebuf.FrameBuffer):
    def __init__(self, width, height, spi, dc, res=None, cs=None):
        self.width = width
        self.height = height
        self.pages = self.height // 8
        self.buffer = bytearray(self.pages * self.width)
        super().__init__(self.buffer, self.width, self.height, framebuf.MONO_VLSB)

        self.spi = spi
        self.dc = dc
        self.res = res
        self.cs = cs

        if self.res is not None:
            self.res.init(self.res.OUT, value=1)
            time.sleep_ms(1)
            self.res(0)
            time.sleep_ms(10)
            self.res(1)

        if self.dc is not None:
            self.dc.init(self.dc.OUT, value=0)
        if self.cs is not None:
            self.cs.init(self.cs.OUT, value=1)

        for cmd in (0xAE, 0x20, 0x00, 0x40, 0xA1, 0xC8, 0xA8, self.height - 1,
                    0xD3, 0x00, 0xDA, 0x12, 0x81, 0xFF, 0xA4, 0xA6, 0xDB, 0x40, 0x8D, 0x14, 0xAF):
            self.write_cmd(cmd)
        self.fill(0)
        self.show()

    def write_cmd(self, cmd):
        if self.cs is not None: self.cs(0)
        self.dc(0)
        self.spi.write(bytearray([cmd]))
        if self.cs is not None: self.cs(1)

    def write_data(self, buf):
        if self.cs is not None: self.cs(0)
        self.dc(1)
        self.spi.write(buf)
        if self.cs is not None: self.cs(1)

    def show(self):
        self.write_cmd(0x21); self.write_cmd(0); self.write_cmd(self.width - 1)
        self.write_cmd(0x22); self.write_cmd(0); self.write_cmd(self.pages - 1)
        self.write_data(self.buffer)


display = SSD1306_SPI(WIDTH, HEIGHT, spi, dc_pin, res_pin, cs_pin)

# --- Sensors (analog pins are all ADC1, so they work with Wi-Fi on) ---
dht_sensor = dht.DHT11(Pin(DHT_PIN))

soil1 = ADC(Pin(SOIL1_PIN)); soil1.atten(ADC.ATTN_11DB)
soil2 = ADC(Pin(SOIL2_PIN)); soil2.atten(ADC.ATTN_11DB)
soil3 = ADC(Pin(SOIL3_PIN)); soil3.atten(ADC.ATTN_11DB)
ldr = ADC(Pin(LDR_PIN)); ldr.atten(ADC.ATTN_11DB)
volt = ADC(Pin(VOLT_PIN)); volt.atten(ADC.ATTN_11DB)

rain_sensor = Pin(RAIN_DO_PIN, Pin.IN)   # DO goes LOW when wet

# --- Outputs ---
pump_relay = Pin(PUMP_PIN, Pin.OUT)
humidifier_relay = Pin(HUMIDIFIER_PIN, Pin.OUT)
amp_audio = PWM(Pin(AUDIO_PIN), freq=1000)

led_red = Pin(LED_RED_PIN, Pin.OUT)
led_blue = Pin(LED_BLUE_PIN, Pin.OUT)
led_green = Pin(LED_GREEN_PIN, Pin.OUT)
led_yellow = Pin(LED_YELLOW_PIN, Pin.OUT)


def audio(on):
    """Speaker tone on/off (works on old and new MicroPython)."""
    try:
        amp_audio.duty_u16(32768 if on else 0)
    except AttributeError:
        amp_audio.duty(512 if on else 0)


# Initial states OFF
pump_relay.value(0)
humidifier_relay.value(0)
audio(False)
led_red.value(0)
led_blue.value(0)
led_green.value(0)
led_yellow.value(0)


# --- Emoji (20x20 face) ---
def draw_eyes(x, y):
    display.fill_rect(x + 5, y + 5, 2, 2, 1)
    display.fill_rect(x + 13, y + 5, 2, 2, 1)


def draw_happy_face(x, y):
    display.rect(x, y, 20, 20, 1)
    draw_eyes(x, y)
    display.pixel(x + 5, y + 12, 1)
    display.pixel(x + 14, y + 12, 1)
    display.hline(x + 6, y + 13, 8, 1)


def draw_sad_face(x, y):
    display.rect(x, y, 20, 20, 1)
    draw_eyes(x, y)
    display.hline(x + 6, y + 12, 8, 1)
    display.pixel(x + 5, y + 13, 1)
    display.pixel(x + 14, y + 13, 1)


print("Smart Garden Master System Initialized.")

# State memory
temp = 0.0
hum = 60.0
soil_dry = False
humidifier_on = False
anim_toggle = False

while True:
    # DHT: keep last good values on failure
    try:
        dht_sensor.measure()
        temp = dht_sensor.temperature()
        hum = dht_sensor.humidity()
    except Exception as e:
        print("DHT Error:", e)

    s1 = soil1.read()
    s2 = soil2.read()
    s3 = soil3.read()
    avg_soil = (s1 + s2 + s3) // 3
    ldr_val = ldr.read()
    voltage = (volt.read() / 4095.0) * 3.3 * 4.0
    raining = rain_sensor.value() == 0

    # Soil moisture percentages
    s1_pct = max(0, min(100, int((4095 - s1) * 100 / 4095)))
    s2_pct = max(0, min(100, int((4095 - s2) * 100 / 4095)))
    s3_pct = max(0, min(100, int((4095 - s3) * 100 / 4095)))

    # Soil state with hysteresis
    if avg_soil > SOIL_DRY_ON:
        soil_dry = True
    elif avg_soil < SOIL_DRY_OFF:
        soil_dry = False

    # Pump: only when dry and not raining
    pump_relay.value(1 if (soil_dry and not raining) else 0)
    led_red.value(1 if soil_dry else 0)

    if soil_dry:
        for _ in range(2):
            audio(True)
            time.sleep_ms(100)
            audio(False)
            time.sleep_ms(100)
        alert_msg = "No Water!"
    else:
        audio(False)
        alert_msg = "Water: OK"

    # Humidifier with hysteresis
    if hum < HUM_LOW_ON:
        humidifier_on = True
    elif hum > HUM_LOW_OFF:
        humidifier_on = False
    humidifier_relay.value(1 if humidifier_on else 0)
    led_blue.value(1 if humidifier_on else 0)

    led_yellow.value(1 if raining else 0)

    # OLED
    display.fill(0)
    display.text("T:{:.1f}C H:{:.0f}%".format(temp, hum), 0, 0)
    display.text("S-1:{}%".format(s1_pct), 0, 11)
    display.text("S-2:{}%".format(s2_pct), 0, 22)
    display.text("S-3:{}%".format(s3_pct), 0, 33)
    display.text("V:{:.1f}V LDR:{}".format(voltage, ldr_val), 0, 44)
    display.text(alert_msg, 0, 54)

    anim_toggle = not anim_toggle
    if soil_dry:
        if anim_toggle:
            draw_sad_face(104, 0)
        else:
            display.rect(104, 0, 20, 20, 1)
    else:
        draw_happy_face(104, 0)

    display.show()
    time.sleep(2)
