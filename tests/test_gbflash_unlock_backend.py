import unittest

import gbflash_provision_8byte_autowchisp as provision
import gbflash_serial_update as serial_update
import gbflash_unlock_backend as backend


class UnlockBackendTests(unittest.TestCase):
    def test_normalize_credential_accepts_common_hex_formats(self):
        expected = bytes.fromhex("12 34 56 78 9A BC DE F0")
        self.assertEqual(backend.normalize_credential("12 34 56 78 9A BC DE F0"), expected)
        self.assertEqual(backend.normalize_credential("12:34:56:78:9a:bc:de:f0"), expected)
        self.assertEqual(backend.normalize_credential("123456789abcdef0"), expected)
        self.assertEqual(backend.normalize_credential("0x123456789ABCDEF0"), expected)

    def test_normalize_credential_rejects_wrong_length(self):
        with self.assertRaises(backend.UnlockError):
            backend.normalize_credential("12 34")

    def test_format_credential(self):
        self.assertEqual(
            backend.format_credential(bytes.fromhex("123456789abcdef0")),
            "12 34 56 78 9A BC DE F0",
        )

    def test_generated_credential_matches_original_provisioning_algorithm(self):
        uid = bytes.fromhex("0FEB50E4C2842154")
        credential = provision.create_credential(uid)
        provision.validate_credential(uid, credential)
        self.assertEqual(backend.format_credential(credential), credential.hex(" ").upper())

    def test_serial_update_packet_format(self):
        packet = serial_update.pack_packet(seq_no=2, command=0x24, payload=b"\x01\x02")
        self.assertEqual(packet[:4], bytes.fromhex("48 48 4A 4A"))
        self.assertEqual(packet[4], 0)
        self.assertEqual(packet[5:7], bytes.fromhex("00 02"))
        self.assertEqual(packet[7:9], bytes.fromhex("00 24"))
        self.assertEqual(packet[9:11], bytes.fromhex("00 02"))
        self.assertEqual(packet[11:13], b"\x01\x02")
        self.assertEqual(packet[13:17], bytes.fromhex("4A 4A 48 48"))

    def test_serial_update_crc16_known_value(self):
        self.assertEqual(serial_update.crc16(b""), 0xFFFF)
        self.assertEqual(serial_update.crc16(bytes.fromhex("01 02 03 04")), 0x2BA1)


if __name__ == "__main__":
    unittest.main()
