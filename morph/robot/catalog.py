"""Purchased-part catalog: actuators, electronics modules, bearings, fasteners.

One source of truth for the keys every module uses (geometry, sizing,
electronics, sourcing, manufacturing). Numbers are typical datasheet values;
`verify: True` marks anything an engineer should confirm with calipers or the
vendor's current datasheet before ordering. Prices are approximate USD, single
quantity, and vendor links are search pages unless the product page is stable.

Owners: the core platform writes the base entries; the electronics/sourcing
module may ADD entries or vendors but must not rename keys.
"""

from __future__ import annotations

from urllib.parse import quote_plus


def search(vendor: str, q: str) -> str:
    qq = quote_plus(q)
    return {
        "digikey": f"https://www.digikey.com/en/products/result?keywords={qq}",
        "mouser": f"https://www.mouser.com/c/?q={qq}",
        "lcsc": f"https://www.lcsc.com/search?q={qq}",
        "amazon": f"https://www.amazon.com/s?k={qq}",
        "aliexpress": f"https://www.aliexpress.com/wholesale?SearchText={qq}",
        "mcmaster": f"https://www.mcmaster.com/products/{qq}",
        "adafruit": f"https://www.adafruit.com/search?q={qq}",
        "sparkfun": f"https://www.sparkfun.com/search/results?term={qq}",
        "robotis": f"https://www.robotis.us/search?q={qq}",
        "seeed": f"https://www.seeedstudio.com/catalogsearch/result/?q={qq}",
        "waveshare": f"https://www.waveshare.com/catalogsearch/result/?q={qq}",
        "pololu": f"https://www.pololu.com/search?query={qq}",
    }[vendor]


def V(vendor: str, q: str, price: float, url: str | None = None, lead_days: int = 3, note: str = "") -> dict:
    return {"vendor": vendor, "url": url or search(vendor, q), "unit_price": price, "lead_days": lead_days, "note": note}


# ---------------------------------------------------------------- actuators
# dims (mm): body l x w x h; shaft_off = shaft axis distance from body centre along l;
# ear_l / ear_w = mounting-hole spacing; horn_d = round horn diameter.
# torque_nm = stall torque at nominal voltage; rated_nm ~ continuous (about 1/3 of stall). torque_v = the maker's stall
# torque at other supply voltages, (V, N·m), where the datasheet gives them; torque_at() reads them.
ACTUATORS = {
    "sg90": dict(name="TowerPro SG90 micro servo", family="hobby_pwm", interface="pwm", protocol="pwm", voltage=(4.8, 6.0), nominal_v=5.0,
                 torque_nm=0.18, rated_nm=0.06, speed_rad_s=10.5, stall_a=0.65, idle_a=0.01, mass_g=9.0, feedback="none",
                 dims=dict(l=22.5, w=12.2, h=22.7, shaft_off=5.9, ear_l=27.8, ear_w=0.0, ear_t=2.5, ear_z=15.9, horn_d=20.0, spline_h=4.0),
                 range_deg=180, verify=True,
                 vendors=[V("amazon", "SG90 micro servo", 3.0), V("adafruit", "micro servo SG90", 5.95, "https://www.adafruit.com/product/169")]),
    "mg996r": dict(name="TowerPro MG996R metal-gear servo", family="hobby_pwm", interface="pwm", protocol="pwm", voltage=(4.8, 7.2), nominal_v=6.0,
                   torque_nm=1.08, torque_v=((4.8, 0.92), (6.0, 1.08)), rated_nm=0.35, speed_rad_s=6.3, stall_a=2.5, idle_a=0.01, mass_g=55.0, feedback="none",
                   dims=dict(l=40.7, w=19.7, h=42.9, shaft_off=10.2, ear_l=49.5, ear_w=10.0, ear_t=2.5, ear_z=28.0, horn_d=24.0, spline_h=4.0),
                   range_deg=180, verify=True,
                   vendors=[V("amazon", "MG996R servo", 9.0), V("aliexpress", "MG996R servo", 4.5, lead_days=14)]),
    "ds3218": dict(name="DSSERVO DS3218 20 kg digital servo", family="hobby_pwm", interface="pwm", protocol="pwm", voltage=(4.8, 6.8), nominal_v=6.8,
                   torque_nm=2.1, rated_nm=0.7, speed_rad_s=6.5, stall_a=2.8, idle_a=0.01, mass_g=60.0, feedback="none",
                   dims=dict(l=40.0, w=20.0, h=40.5, shaft_off=10.0, ear_l=49.5, ear_w=10.0, ear_t=2.5, ear_z=27.0, horn_d=24.0, spline_h=4.0),
                   range_deg=270, verify=True,
                   vendors=[V("amazon", "DS3218 servo", 16.0)]),
    "sts3215": dict(name="Feetech STS3215 serial bus servo (12 V, 30 kg·cm)", family="smart_bus", interface="ttl_half_duplex",
                    protocol="feetech_scs",
                    voltage=(6.0, 12.6), nominal_v=12.0, torque_nm=2.9, rated_nm=0.95, speed_rad_s=4.8, stall_a=2.7, idle_a=0.06,
                    mass_g=55.0, feedback="12-bit magnetic encoder, load, voltage, temperature",
                    dims=dict(l=45.2, w=24.7, h=35.0, shaft_off=12.0, ear_l=0.0, ear_w=0.0, ear_t=0.0, ear_z=0.0, horn_d=22.0, spline_h=3.0),
                    range_deg=360, verify=True, note="the SO-100/SO-101 arm actuator; 7.4 V variant gives ~19.5 kg·cm",
                    vendors=[V("seeed", "STS3215 servo", 17.0), V("aliexpress", "Feetech STS3215 12V", 14.0, lead_days=14)]),
    "xl430_w250": dict(name="ROBOTIS DYNAMIXEL XL430-W250-T", family="dynamixel", interface="ttl_half_duplex", protocol="dynamixel_2",
                       voltage=(6.5, 12.0), nominal_v=12.0, torque_nm=1.5, torque_v=((9.0, 1.0), (11.1, 1.4), (12.0, 1.5)), rated_nm=0.5, speed_rad_s=6.1, stall_a=1.4, idle_a=0.05,
                       mass_g=57.2, feedback="12-bit absolute encoder, current, temperature, voltage",
                       dims=dict(l=46.5, w=28.5, h=34.0, shaft_off=11.25, ear_l=0.0, ear_w=0.0, ear_t=0.0, ear_z=0.0, horn_d=20.0, spline_h=2.0),
                       range_deg=360, verify=True,
                       vendors=[V("robotis", "XL430-W250-T", 49.9, "https://www.robotis.us/dynamixel-xl430-w250-t/")]),
    "xm430_w350": dict(name="ROBOTIS DYNAMIXEL XM430-W350-T", family="dynamixel", interface="ttl_half_duplex", protocol="dynamixel_2",
                       voltage=(10.0, 14.8), nominal_v=12.0, torque_nm=4.1, torque_v=((11.1, 3.8), (12.0, 4.1)), rated_nm=1.4, speed_rad_s=4.8, stall_a=2.3, idle_a=0.05,
                       mass_g=82.0, feedback="12-bit absolute encoder, current, temperature, voltage",
                       dims=dict(l=46.5, w=28.5, h=34.0, shaft_off=11.25, ear_l=0.0, ear_w=0.0, ear_t=0.0, ear_z=0.0, horn_d=20.0, spline_h=2.0),
                       range_deg=360, verify=True,
                       vendors=[V("robotis", "XM430-W350-T", 289.9, "https://www.robotis.us/dynamixel-xm430-w350-t/")]),
}

FAMILY_ACTUATORS = {  # weakest -> strongest; sizing picks the first that meets torque x margin
    "hobby_pwm": ["sg90", "mg996r", "ds3218"],
    "smart_bus": ["sts3215"],
    "dynamixel": ["xl430_w250", "xm430_w350"],
}

# The command protocol an actuator speaks. Feetech and DYNAMIXEL servos share the half-duplex TTL wiring but not the
# commands, so one bus (and its firmware) drives one protocol.
PROTOCOLS = {"pwm": "hobby PWM", "feetech_scs": "Feetech SCS/STS", "dynamixel_2": "DYNAMIXEL Protocol 2.0"}
FAMILY_PROTOCOL = {"hobby_pwm": "pwm", "smart_bus": "feetech_scs", "smart_bus_7v4": "feetech_scs", "dynamixel": "dynamixel_2"}


def protocol(key: str) -> str | None:
    """The protocol an actuator speaks: its own entry's, else its family's (entries registered at runtime), else None."""
    a = ACTUATORS.get(key)
    if a is None:
        return None
    return a.get("protocol") or FAMILY_PROTOCOL.get(a.get("family", "")) or ("pwm" if a.get("interface") == "pwm" else None)

def torque_at(key: str, volts: float) -> float:
    """Stall torque (N·m) of actuator `key` on a `volts` supply: between the datasheet's points (torque_v, or just the
    nominal one) by straight lines, in proportion to the voltage below the lowest (a DC servo's stall current, and so its
    torque, follows the voltage), and never more than the highest point above it (no credit past its rating)."""
    a = ACTUATORS[key]
    pts = sorted(a.get("torque_v") or ((a["nominal_v"], a["torque_nm"]),))
    if volts >= pts[-1][0]:
        return float(pts[-1][1])
    if volts <= pts[0][0]:
        return float(pts[0][1]) * max(float(volts), 0.0) / pts[0][0]
    for (v0, t0), (v1, t1) in zip(pts, pts[1:]):
        if v0 <= volts <= v1:
            return float(t0 + (t1 - t0) * (volts - v0) / (v1 - v0))
    return float(pts[-1][1])


# ---------------------------------------------------------------- electronics modules
# dims (mm): board l x w x component height h. pins: logical pin names used by schematic/harness.
MODULES = {
    "arduino_nano": dict(name="Arduino Nano (ATmega328P)", role="controller", dims=dict(l=45.0, w=18.0, h=8.0), logic_v=5.0,
                         supply=(7.0, 12.0), current_a=0.05, pwm_channels=6, uarts=1, adc=8, wireless="none", mass_g=7.0,
                         vendors=[V("digikey", "Arduino Nano A000005", 24.9), V("amazon", "Arduino Nano clone", 5.0)]),
    "esp32_devkit": dict(name="ESP32-DevKitC-32E", role="controller", dims=dict(l=55.0, w=28.0, h=9.0), logic_v=3.3,
                         supply=(5.0, 5.0), current_a=0.25, pwm_channels=16, uarts=3, adc=18, wireless="wifi+bt", mass_g=10.0,
                         vendors=[V("digikey", "ESP32-DevKitC-32E", 10.0), V("mouser", "ESP32-DevKitC-32E", 10.0)]),
    "esp32_s3_devkit": dict(name="ESP32-S3-DevKitC-1", role="controller", dims=dict(l=63.0, w=25.5, h=9.0), logic_v=3.3,
                            supply=(5.0, 5.0), current_a=0.3, pwm_channels=8, uarts=3, adc=20, wireless="wifi+ble", mass_g=11.0,
                            vendors=[V("digikey", "ESP32-S3-DevKitC-1-N8R8", 15.0)]),
    "rp2040_pico": dict(name="Raspberry Pi Pico", role="controller", dims=dict(l=51.0, w=21.0, h=4.0), logic_v=3.3,
                        supply=(1.8, 5.5), current_a=0.03, pwm_channels=16, uarts=2, adc=3, wireless="none", mass_g=3.0,
                        vendors=[V("digikey", "Raspberry Pi Pico SC0915", 4.0), V("adafruit", "Raspberry Pi Pico", 4.0)]),
    "pca9685_board": dict(name="Adafruit 16-channel 12-bit PWM driver (PCA9685)", role="servo_driver", dims=dict(l=62.2, w=25.4, h=10.0),
                          logic_v=5.0, channels=16, interface="i2c", mass_g=9.0,
                          vendors=[V("adafruit", "PCA9685 16-channel", 14.95, "https://www.adafruit.com/product/815"),
                                   V("amazon", "PCA9685 servo driver board", 4.0)]),
    "bus_servo_adapter": dict(name="Waveshare Bus Servo Adapter (A)", role="servo_driver", dims=dict(l=42.0, w=33.0, h=12.0),
                              interface="usb+uart_to_ttl_half_duplex", mass_g=10.0, verify=True,
                              vendors=[V("waveshare", "Bus Servo Adapter", 6.0), V("amazon", "Waveshare bus servo adapter", 9.0)]),
    "u2d2": dict(name="ROBOTIS U2D2 + Power Hub", role="servo_driver", dims=dict(l=48.0, w=18.0, h=14.0), interface="usb_to_ttl_rs485",
                 mass_g=9.0, vendors=[V("robotis", "U2D2", 32.1, "https://www.robotis.us/u2d2/")]),
    "buck_5v": dict(name="5 V 3 A step-down regulator (logic rail)", role="power", dims=dict(l=22.0, w=17.0, h=6.0), out_v=5.0, out_a=3.0,
                    mass_g=3.0, vendors=[V("pololu", "5V 3A step-down regulator D36V28F5", 9.95)]),
    "buck_servo": dict(name="Adjustable 20 A step-down module (servo rail)", role="power", dims=dict(l=60.0, w=51.0, h=22.0),
                       out_v=6.0, out_a=20.0, mass_g=60.0, verify=True, vendors=[V("amazon", "20A DC-DC buck converter 300W", 12.0)]),
    "psu_12v": dict(name="12 V 10 A desktop power supply", role="supply", dims=dict(l=150.0, w=60.0, h=35.0), out_v=12.0, out_a=10.0,
                    mass_g=400.0, external=True, vendors=[V("amazon", "12V 10A power supply 5.5x2.1", 22.0), V("digikey", "12V 120W desktop adapter", 35.0)]),
    "psu_6v": dict(name="6 V 10 A power supply", role="supply", dims=dict(l=150.0, w=60.0, h=35.0), out_v=6.0, out_a=10.0,
                   mass_g=400.0, external=True, vendors=[V("amazon", "6V 10A power supply", 20.0)]),
    "fsr402": dict(name="Interlink FSR 402 force-sensing resistor", role="sensor", dims=dict(l=18.3, w=18.3, h=0.45, tail=37.0),
                   range_n=(0.2, 20.0), mass_g=0.5,
                   vendors=[V("adafruit", "FSR 402 round", 7.95, "https://www.adafruit.com/product/166"), V("digikey", "Interlink 30-81794", 7.0)]),
    "wrist_camera": dict(name="USB UVC camera module 1080p, 32 x 32 mm", role="sensor", dims=dict(l=32.0, w=32.0, h=18.0), mass_g=12.0,
                         verify=True, vendors=[V("amazon", "32mm USB camera module 1080p UVC", 25.0)]),
    "imu": dict(name="BNO085 9-DoF IMU breakout", role="sensor", dims=dict(l=25.4, w=20.3, h=4.0), interface="i2c", mass_g=3.0,
                vendors=[V("adafruit", "BNO085", 24.95)]),
    "estop": dict(name="22 mm latching emergency-stop switch", role="safety", dims=dict(l=40.0, w=40.0, h=45.0), mass_g=60.0,
                  vendors=[V("amazon", "22mm emergency stop button", 8.0)]),
    "usb_c_breakout": dict(name="USB-C panel-mount extension", role="connector", dims=dict(l=30.0, w=12.0, h=8.0), mass_g=5.0,
                           vendors=[V("adafruit", "panel mount USB C extension", 7.95)]),
    "xt30_inlet": dict(name="XT30 / 5.5x2.1 barrel power inlet", role="connector", dims=dict(l=15.0, w=10.0, h=12.0), mass_g=3.0,
                       vendors=[V("amazon", "5.5x2.1 panel mount DC jack", 1.0)]),
}

BEARINGS = {
    "6806": dict(name="6806-2RS deep-groove ball bearing 30x42x7", d=30.0, D=42.0, B=7.0, mass_g=27.0,
                 vendors=[V("amazon", "6806-2RS bearing", 4.0), V("mcmaster", "6806 ball bearing", 12.0)]),
    "6808": dict(name="6808-2RS thin-section bearing 40x52x7", d=40.0, D=52.0, B=7.0, mass_g=32.0,
                 vendors=[V("amazon", "6808-2RS bearing", 5.0)]),
    "608": dict(name="608-2RS bearing 8x22x7", d=8.0, D=22.0, B=7.0, mass_g=12.0, vendors=[V("amazon", "608-2RS bearing", 0.8)]),
    "lazy_susan_100": dict(name="100 mm lazy-susan turntable bearing", d=60.0, D=100.0, B=8.0, mass_g=120.0,
                           vendors=[V("amazon", "4 inch lazy susan bearing", 5.0)]),
}

# metric socket-head cap screws and friends: (head d, head h, hex key) by size
FASTENER_SIZES = {"M2": (3.8, 2.0, 1.5), "M2.5": (4.5, 2.5, 2.0), "M3": (5.5, 3.0, 2.5), "M4": (7.0, 4.0, 3.0), "M5": (8.5, 5.0, 4.0)}
FASTENER_TORQUE_NM = {"M2": 0.2, "M2.5": 0.4, "M3": 0.8, "M4": 1.8, "M5": 3.5}  # into plastic / insert, conservative


def fastener_vendor(spec: str) -> list:
    return [V("mcmaster", f"socket head screw {spec}", 0.12), V("amazon", f"{spec} socket head cap screw assortment", 0.05)]


def get(key: str) -> dict:
    for table in (ACTUATORS, MODULES, BEARINGS):
        if key in table:
            return {"key": key, **table[key]}
    raise KeyError(f"unknown catalog key: {key}")


def price(key: str) -> float:
    vs = get(key).get("vendors") or []
    return min(v["unit_price"] for v in vs) if vs else 0.0


# ---------------------------------------------------------------- electronics2 additions (appended; keys are new)
# Controller options and the generated custom controller board. `external` parts are not placed in the base bay.
MODULES.update({
    "morph_controller_pcba": dict(name="Morph controller PCBA (ESP32-S3-WROOM-1, generated)", role="controller",
                                  dims=dict(l=92.0, w=66.0, h=14.0), logic_v=3.3, supply=(6.0, 14.0), current_a=0.35,
                                  wireless="wifi+ble", mass_g=28.0, verify=True,
                                  note="size comes from eplan.custom_pcb.size_mm; price from the generated JLCPCB BOM",
                                  vendors=[V("jlcpcb", "PCBA", 24.0, "https://jlcpcb.com/quote", lead_days=10,
                                             note="order with gerbers.zip + jlc_bom.csv + jlc_cpl.csv")]),
    "openrb_150": dict(name="ROBOTIS OpenRB-150 (SAMD21, 4x DYNAMIXEL TTL ports)", role="controller",
                       dims=dict(l=66.0, w=25.0, h=12.0), logic_v=3.3, supply=(5.0, 12.0), current_a=0.05,
                       pwm_channels=8, uarts=2, adc=7, wireless="none", mass_g=9.0, verify=True,
                       vendors=[V("robotis", "OpenRB-150", 39.9, "https://www.robotis.us/openrb-150/")]),
    "fuse_inline": dict(name="Inline ATO blade-fuse holder, 16 AWG, with 10 A fuse", role="protection",
                        dims=dict(l=60.0, w=20.0, h=12.0), mass_g=12.0, external=True,
                        vendors=[V("amazon", "inline ATO fuse holder 16 AWG", 1.5), V("digikey", "Littelfuse 0FHA0001ZXJ", 4.2)]),
})
MODULES.update({
    "psu_6v20a": dict(name="6 V 20 A (120 W) enclosed switching supply, adjustable 5.4-6.6 V", role="supply",
                      dims=dict(l=160.0, w=98.0, h=38.0), out_v=6.0, out_a=20.0, mass_g=550.0, external=True, verify=True,
                      vendors=[V("amazon", "6V 20A 120W switching power supply", 24.0)]),
})
MODULES.update({
    "psu_12v20a": dict(name="12 V 20 A (240 W) enclosed switching supply", role="supply", dims=dict(l=200.0, w=98.0, h=40.0),
                       out_v=12.0, out_a=20.0, mass_g=700.0, external=True, verify=True,
                       vendors=[V("amazon", "12V 20A 240W switching power supply", 26.0),
                                V("digikey", "Mean Well LRS-350-12", 38.0)]),
})
