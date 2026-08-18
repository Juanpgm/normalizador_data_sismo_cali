from extraer_coords_uas import parse_srt_gps

MAVIC = """1
00:00:00,000 --> 00:00:00,033
<font size="28">FrameCnt: 1, DiffTime: 33ms
2026-08-10 14:22:31.123
[iso: 100] [shutter: 1/640] [fnum: 2.8] [ev: 0] [color_md: default]
[latitude: 3.441523] [longitude: -76.520432] [rel_alt: 42.100 abs_alt: 998.312]</font>
"""

PHANTOM = """1
00:00:00,000 --> 00:00:01,000
GPS(-76.520432,3.441523,19) BAROMETER:42.1
"""


def test_mavic_labeled():
    lat, lon, alt = parse_srt_gps(MAVIC)
    assert (lat, lon, alt) == (3.441523, -76.520432, 42.100)


def test_phantom_lon_lat_order():
    lat, lon, _ = parse_srt_gps(PHANTOM)
    assert (lat, lon) == (3.441523, -76.520432)


def test_no_gps_and_null_island():
    assert parse_srt_gps("1\n00:00:00,000 --> 00:00:01,000\nhola\n") is None
    assert parse_srt_gps("[latitude: 0.000000] [longitude: 0.000000]") is None
