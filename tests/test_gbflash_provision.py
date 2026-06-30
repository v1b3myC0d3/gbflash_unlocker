import unittest

import gbflash_provision_8byte_autowchisp as gp


class CredentialAlgorithmTests(unittest.TestCase):
    def test_known_uid_generates_confirmed_8byte_credential(self):
        uid = bytes.fromhex("0FEB50E4C2842154")
        credential = gp.create_credential(uid)
        self.assertEqual(credential.hex().upper(), "5115005070A3B26F")
        gp.validate_credential(uid, credential)

    def test_credential_is_two_little_endian_32bit_rsa_signatures(self):
        uid = bytes.fromhex("0FEB50E4C2842154")
        credential = gp.create_credential(uid)
        message = uid[:5] + uid[:1]

        for block_index, credential_offset in enumerate((0, 4)):
            signature = int.from_bytes(
                credential[credential_offset : credential_offset + 4], "little"
            )
            decoded = pow(signature, gp.E, gp.N)
            expected_block = message[block_index * 3 : block_index * 3 + 3]
            self.assertEqual(decoded, int.from_bytes(expected_block, "big"))

    def test_validate_credential_rejects_wrong_uid(self):
        credential = gp.create_credential(bytes.fromhex("0FEB50E4C2842154"))
        with self.assertRaises(gp.ProvisioningError):
            gp.validate_credential(bytes.fromhex("10EB50E4C2842154"), credential)

    def test_validate_credential_rejects_legacy_16byte_record(self):
        legacy_record = bytes.fromhex("511500500000000070A3B26F00000000")
        with self.assertRaises(gp.ProvisioningError):
            gp.validate_credential(bytes.fromhex("0FEB50E4C2842154"), legacy_record)

    def test_parse_uid_accepts_wchisp_info_format(self):
        output = """
        Chip model: CH579
        Chip UID: 0F-EB-50-E4-C2-84-21-54
        """
        self.assertEqual(gp.parse_uid(output), bytes.fromhex("0FEB50E4C2842154"))

    def test_key_parameters_are_self_consistent(self):
        gp.verify_key_parameters()


class UtilityTests(unittest.TestCase):
    def test_first_difference_reports_credential_offset(self):
        expected = b"\x51\x15\x00\x50"
        actual = b"\x51\x15\x01\x50"
        self.assertIn("0x0002", gp.first_difference(expected, actual))


if __name__ == "__main__":
    unittest.main()
