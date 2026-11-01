"""Public FIPS 197 and synthetic buffers only; no local files or memory scans."""
import random
import unittest

from wechat_export_tool.aes128_schedule import expand_aes128_key, scan_aes128_schedules

KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
FIPS_SCHEDULE = bytes.fromhex(
    "000102030405060708090a0b0c0d0e0f"
    "d6aa74fdd2af72fadaa678f1d6ab76fe"
    "b692cf0b643dbdf1be9bc5006830b3fe"
    "b6ff744ed2c2c9bf6c590cbf0469bf41"
    "47f7f7bc95353e03f96c32bcfd058dfd"
    "3caaa3e8a99f9deb50f3af57adf622aa"
    "5e390f7df7a69296a7553dc10aa31f6b"
    "14f9701ae35fe28c440adf4d4ea9c026"
    "47438735a41c65b9e016baf4aebf7ad2"
    "549932d1f08557681093ed9cbe2c974e"
    "13111d7fe3944a17f307a78b4d2b30c5"
)


def multiply(a, b):
    result = 0
    for _ in range(8):
        if b & 1:
            result ^= a
        a = ((a << 1) ^ (0x11b if a & 128 else 0)) & 255
        b >>= 1
    return result


def inverse_mix(key):
    output = bytearray()
    for start in range(0, 16, 4):
        column = key[start:start + 4]
        for row in range(4):
            output.append(multiply(14, column[row]) ^ multiply(11, column[(row + 1) % 4]) ^
                          multiply(13, column[(row + 2) % 4]) ^ multiply(9, column[(row + 3) % 4]))
    return bytes(output)


def inverse_schedule(encryption):
    rounds = [encryption[start:start + 16] for start in range(0, 176, 16)]
    return b"".join([rounds[-1], *(inverse_mix(key) for key in rounds[-2:0:-1]), rounds[0]])


def swapped(schedule):
    return b"".join(schedule[start:start + 4][::-1] for start in range(0, 176, 4))


class Aes128ScheduleTests(unittest.TestCase):
    def test_fips_full_schedule_and_every_alignment(self):
        self.assertEqual(len(FIPS_SCHEDULE), 176)
        self.assertEqual(expand_aes128_key(KEY), FIPS_SCHEDULE)
        for offset in range(32):
            with self.subTest(offset=offset):
                self.assertEqual(list(scan_aes128_schedules(b"\xab" * offset + FIPS_SCHEDULE + b"\xcd" * 31)), [KEY])

    def test_word_swapped_encryption(self):
        self.assertEqual(list(scan_aes128_schedules(b"\xff" * 7 + swapped(FIPS_SCHEDULE))), [KEY])
        self.assertEqual(list(scan_aes128_schedules(swapped(FIPS_SCHEDULE), include_word_swapped=False)), [])

    def test_equivalent_inverse_and_word_swapped_inverse(self):
        inverse = inverse_schedule(FIPS_SCHEDULE)
        for representation in (inverse, swapped(inverse)):
            self.assertEqual(list(scan_aes128_schedules(b"\x9f" * 13 + representation + b"\xc3")), [KEY])
        self.assertEqual(list(scan_aes128_schedules(inverse, include_decrypt=False)), [])

    def test_only_full_44_word_verification_can_accept(self):
        corrupted = bytearray(FIPS_SCHEDULE)
        corrupted[-1] ^= 1
        self.assertEqual(list(scan_aes128_schedules(bytes(corrupted))), [])
        self.assertEqual(list(scan_aes128_schedules(FIPS_SCHEDULE[:-1])), [])
        self.assertEqual(list(scan_aes128_schedules(random.Random(197).randbytes(8192))), [])

    def test_multiple_keys_and_duplicate_representations(self):
        second = b"ScheduleKey12345"
        payload = (b"\xa1" * 3 + FIPS_SCHEDULE + b"\x98" * 37 +
                   inverse_schedule(expand_aes128_key(second)) + b"\xa3" * 19 + swapped(FIPS_SCHEDULE))
        self.assertEqual(set(scan_aes128_schedules(payload)), {KEY, second})


if __name__ == "__main__":
    unittest.main()
