"""Extra catalog entries for imported open-source robots (SO-101 / LeKiwi family).

Registered into morph.robot.catalog on import, so the core catalog file stays
owned by its module. Values from the SO-ARM100 / LeKiwi BOMs and vendor pages;
`verify` marks numbers that differ between sellers.
"""

from . import catalog
from .catalog import V

catalog.ACTUATORS.setdefault("sts3215_7v4", dict(
    name="Feetech STS3215-C001 serial bus servo (7.4 V, 1/345, ~19.5 kg·cm)", family="smart_bus",
    interface="ttl_half_duplex", protocol="feetech_scs", voltage=(4.8, 7.4), nominal_v=7.4, torque_nm=1.91, torque_v=((6.0, 1.62), (7.4, 1.91)), rated_nm=0.6,
    speed_rad_s=4.8, stall_a=2.4, idle_a=0.06, mass_g=55.0, feedback="12-bit magnetic encoder, load, voltage, temperature",
    dims=dict(l=45.2, w=24.7, h=35.0, shaft_off=12.0, ear_l=0.0, ear_w=0.0, ear_t=0.0, ear_z=0.0, horn_d=22.0, spline_h=3.0),
    range_deg=360, verify=True, note="SO-101 follower servo; the leader arm uses mixed gear ratios",
    vendors=[V("aliexpress", "Feetech STS3215 C001 7.4V", 13.89, lead_days=14), V("seeed", "STS3215 servo 7.4V", 16.0)]))
catalog.ACTUATORS.setdefault("xl330_m288", dict(
    name="ROBOTIS DYNAMIXEL XL330-M288-T", family="dynamixel", interface="ttl_half_duplex", protocol="dynamixel_2",
    voltage=(3.7, 6.0), nominal_v=5.0, torque_nm=0.52, rated_nm=0.17, speed_rad_s=10.0, stall_a=1.47, idle_a=0.03, mass_g=18.0,
    feedback="12-bit absolute encoder, current", dims=dict(l=34.0, w=20.0, h=26.0, shaft_off=8.0, ear_l=0.0, ear_w=0.0,
    ear_t=0.0, ear_z=0.0, horn_d=16.0, spline_h=2.0), range_deg=360, verify=True,
    vendors=[V("robotis", "XL330-M288-T", 23.9, "https://www.robotis.us/dynamixel-xl330-m288-t/")]))
catalog.FAMILY_ACTUATORS.setdefault("smart_bus_7v4", ["sts3215_7v4", "sts3215"])

for key, entry in {
    "seeed_bus_adapter": dict(name="Seeed Studio bus servo driver board (SO-101 kit)", role="servo_driver",
                              dims=dict(l=42.0, w=33.0, h=12.0), interface="usb+uart_to_ttl_half_duplex", mass_g=10.0,
                              verify=True, vendors=[V("seeed", "bus servo driver board", 9.9)]),
    "psu_5v4a": dict(name="5 V 4 A power supply, 5.5x2.1 mm barrel (SO-101 follower)", role="supply",
                     dims=dict(l=90.0, w=45.0, h=30.0), out_v=5.0, out_a=4.0, mass_g=200.0, external=True,
                     vendors=[V("amazon", "5V 4A power supply 5.5x2.1", 10.0, "https://www.amazon.com/dp/B087LY41PV")]),
    "psu_12v5a": dict(name="12 V 5 A power supply, 5.5x2.1 mm barrel", role="supply", dims=dict(l=110.0, w=50.0, h=32.0),
                      out_v=12.0, out_a=5.0, mass_g=300.0, external=True,
                      vendors=[V("amazon", "12V 5A power supply 5.5x2.1", 13.0)]),
    "raspberry_pi_5": dict(name="Raspberry Pi 5 (4 GB)", role="computer", dims=dict(l=85.0, w=56.0, h=17.0), logic_v=3.3,
                           supply=(5.0, 5.0), current_a=3.0, wireless="wifi+bt", mass_g=46.0,
                           vendors=[V("adafruit", "Raspberry Pi 5 4GB", 60.0, "https://www.adafruit.com/product/5812")]),
    "battery_12v": dict(name="12 V 5 Ah Li-ion battery pack (DC 5.5x2.1)", role="supply", dims=dict(l=110.0, w=65.0, h=24.0),
                        out_v=12.0, out_a=5.0, mass_g=350.0, verify=True, vendors=[V("amazon", "12V 5000mAh lithium battery pack DC", 32.99)]),
    "dc_dc_12v_5v_usbc": dict(name="12 V to 5 V 5 A USB-C step-down (Pi power)", role="power", dims=dict(l=60.0, w=25.0, h=15.0),
                              out_v=5.0, out_a=5.0, mass_g=40.0, vendors=[V("amazon", "12V to 5V 5A USB-C converter", 9.99)]),
    "usb_camera": dict(name="USB UVC camera 1080p (front/wrist)", role="sensor", dims=dict(l=32.0, w=32.0, h=18.0), mass_g=12.0,
                       vendors=[V("amazon", "1080p USB camera module", 12.98)]),
    "usb_c_cable_2pack": dict(name="USB-C cables (2-pack)", role="connector", dims=dict(l=40.0, w=10.0, h=6.0), mass_g=40.0,
                              vendors=[V("amazon", "USB C cable 2 pack", 7.0)]),
}.items():
    catalog.MODULES.setdefault(key, entry)
