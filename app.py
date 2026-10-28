from machine import Pin, SPI, ADC
import time
import dht
import framebuf  # fixed typo (was "frameab")

# --- SPI OLED Pin Configurations (MOSI:23, CLK:18, RES:2, DC:19, CS:21) ---
spi = SPI(1, baudrate=10000000, polarity=0, phase=0, sck=Pin(18), mosi=Pin(23))
res_pin = Pin(2, Pin.OUT)
dc_pin = Pin(19, Pin.OUT)
cs_pin = Pin(21, Pin.OUT)

WIDTH = 128
HEIGHT = 64

# --- Thresholds (with hysteresis) ---
SOIL_DRY_ON = 2600     # pump turns ON above this
SOIL_DRY_OFF = 2400    # pump turns OFF below this
HUM_LOW_ON = 58.0      # humidifier turns ON below this
HUM_LOW_OFF = 62.0     # humidifier turns OFF above this
RAIN_THRESHOLD = 2000  # below this = raining


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


# Initialize Display
display = SSD1306_SPI(WIDTH, HEIGHT, spi, dc_pin, res_pin, cs_pin)

# --- Sensor Setup ---
dht_sensor = dht.DHT22(Pin(15))

soil1 = ADC(Pin(32)); soil1.atten(ADC.ATTN_11DB)
soil2 = ADC(Pin(33)); soil2.atten(ADC.ATTN_11DB)
soil3 = ADC(Pin(34)); soil3.atten(ADC.ATTN_11DB)

ldr = ADC(Pin(35)); ldr.atten(ADC.ATTN_11DB)
rain_sensor = ADC(Pin(36)); rain_sensor.atten(ADC.ATTN_11DB)
volt = ADC(Pin(39)); volt.atten(ADC.ATTN_11DB)

# --- Control & Output Pins ---
pump_relay = Pin(27, Pin.OUT)
humidifier_relay = Pin(14, Pin.OUT)
buzzer = Pin(26, Pin.OUT)

led_red = Pin(25, Pin.OUT)   # moved from GPIO12 (boot-strapping pin)
led_blue = Pin(13, Pin.OUT)
led_green = Pin(4, Pin.OUT)
led_yellow = Pin(16, Pin.OUT)

# Initial states OFF
pump_relay.value(0)
humidifier_relay.value(0)
buzzer.value(0)
led_red.value(0)
led_blue.value(0)
led_green.value(0)
led_yellow.value(0)

# State memory
temp = 0.0
hum = 60.0          # neutral default so a failed first read doesn't trigger the humidifier
pump_on = False
humidifier_on = False

print("Smart Garden System Initialized Successfully.")

while True:
    # Keep last good values if the DHT read fails
    try:
        dht_sensor.measure()
        temp = dht_sensor.temperature()
        hum = dht_sensor.humidity()
    except Exception as e:
        print("DHT Error:", e)

    avg_soil = (soil1.read() + soil2.read() + soil3.read()) // 3
    rain_val = rain_sensor.read()
    voltage = (volt.read() / 4095.0) * 3.3 * 4.0
    raining = rain_val < RAIN_THRESHOLD

    # Automation logic with hysteresis
    if raining or avg_soil < SOIL_DRY_OFF:
        pump_on = False
    elif avg_soil > SOIL_DRY_ON:
        pump_on = True

    if hum < HUM_LOW_ON:
        humidifier_on = True
    elif hum > HUM_LOW_OFF:
        humidifier_on = False

    pump_relay.value(1 if pump_on else 0)
    led_red.value(1 if pump_on else 0)
    humidifier_relay.value(1 if humidifier_on else 0)
    led_blue.value(1 if humidifier_on else 0)
    led_yellow.value(1 if raining else 0)

    # Status texts
    soil_status = "Soil: Dry!" if avg_soil > SOIL_DRY_ON else "Soil: Good"
    hum_status = "Air: Dry!" if humidifier_on else "Air: Fresh"
    rain_status = "Rain: Yes" if raining else "Rain: No"
    bat_status = "Bat: Low" if (6.4 > voltage > 2.0) else "Bat: OK"

    # OLED display update
    display.fill(0)
    display.text("Temp: {:.1f}C".format(temp), 0, 0)
    display.text("Hum: {:.1f}%".format(hum), 0, 10)
    display.text(soil_status, 0, 20)
    display.text(hum_status, 0, 30)
    display.text(rain_status, 0, 40)
    display.text(bat_status, 0, 52)
    display.show()

    time.sleep(2)
