# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the desktop editor's binary round trip with synthetic r7 records."""

from pathlib import Path
import tempfile
import unittest

from tools.classic_p25_editor import Codeplug, Fields


def image():
    data = bytearray(b"\xff" * 0x120020)
    for bitmap in (0x3780, *(0x9B1B0 + bank * 0x1C10 for bank in range(7))):
        data[bitmap:bitmap + 16] = bytes(16)
    # Slots 1–4 and 1024 are in use. Slot 5 contains a deleted P25 record.
    data[0x3780] = 0x0F
    data[0x9B1B0 + 6 * 0x1C10 + 15] = 0x80
    records = (0x3790, 0x37C8, 0x3800, 0x3838, 0x3870, 0xA75E8)
    # 438.500 MHz RX/TX. Unrelated bytes are nonzero to detect flag damage.
    for n, offset in enumerate(records):
        record = bytearray(56)
        record[:16] = f"Channel {n + 1}".encode().ljust(16, b"\xff")
        record[0x10:0x18] = b"\x00\x00\x85\x43" * 2
        record[0x18] = (0xA5, 0x00, 0x01, 0xA5, 0xA5, 0xA5)[n]
        record[0x26:0x2A] = b"\x92\x01\xE2\x40"
        record[0x2B:0x2D] = b"\x93\xD2"
        record[0x2E:0x30] = b"\x34\x12"
        record[0x33] = 0x04 if n == 5 else 0
        data[offset:offset + 56] = record
    data[-8:] = b"trailing"
    return data


class P25EditorTest(unittest.TestCase):
    def test_zero_changes_and_exclusive_save(self):
        original = image()
        codeplug = Codeplug(original)
        self.assertEqual([c.number for c in codeplug.channels()], [1, 4, 1024])
        self.assertFalse(codeplug.edit(1, Fields(0x293, 4660, 123456)))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "copy.bin"
            codeplug.save_copy(path)
            self.assertEqual(path.read_bytes(), original)
            with self.assertRaises(FileExistsError):
                codeplug.save_copy(path)
            self.assertEqual(path.read_bytes(), original)

    def test_edit_reload_and_compare_every_byte(self):
        original = image()
        codeplug = Codeplug(original)
        transmit = Fields.parse("0xABC", "65535", "9999999")
        receive = Fields.parse("F7E", "0", "0")
        codeplug.edit(1, transmit)
        codeplug.edit(1024, receive)
        expected = bytearray(original)
        expected[0x37B7:0x37BA] = b"\x98\x96\x7F"
        expected[0x37BB:0x37BD] = b"\xBC\xDA"
        expected[0x37BE:0x37C0] = b"\xFF\xFF"
        expected[0xA760E:0xA7612] = b"\x12\x00\x16\x00"
        expected[0xA7613:0xA7615] = b"\x7E\xDF"
        expected[0xA7616:0xA7618] = b"\x00\x00"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "copy.bin"
            codeplug.save_copy(path)
            saved = path.read_bytes()
        self.assertEqual(saved, expected)
        reloaded = Codeplug(saved)
        self.assertEqual(reloaded.channel(1).fields, transmit)
        self.assertEqual(reloaded.channel(4), Codeplug(original).channel(4))
        self.assertEqual(reloaded.channel(1024).fields, receive)

    def test_rejected_edits_leave_fields_unchanged(self):
        codeplug = Codeplug(image())
        for fields in (Fields(0x1000, 1, 1), Fields(0xF7F, 1, 1), Fields(0xF7E, 1, 1),
                       Fields(1, 0, 1), Fields(1, 65536, 1), Fields(1, 1, 0), Fields(1, 1, 10000000)):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                codeplug.edit(1, fields)
        self.assertEqual(codeplug.channel(1).fields, Fields(0x293, 4660, 123456))
        with self.assertRaises(ValueError):
            codeplug.edit(1024, Fields(0xF7F, 0, 0))
        for text in (("12G", "1", "1"), ("293", "1.5", "1"), ("293", "1", "-1")):
            with self.subTest(text=text), self.assertRaises(ValueError):
                Fields.parse(*text)

    def test_reject_non_codeplug(self):
        for data in (b"", bytes(0x120000), b"PK\x03\x04" + bytes(0x120000)):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                Codeplug(data)

    def test_split_nac_round_trip_and_restore_follow_rx(self):
        original = image()
        codeplug = Codeplug(original)
        split = Fields.parse("F7E", "4660", "123456", "000")
        self.assertTrue(codeplug.edit(1, split))
        for tx in (0xF7E, 0xF7F, 0x1000):
            with self.subTest(tx=tx), self.assertRaises(ValueError):
                codeplug.edit(1, Fields(0xF7E, 4660, 123456, tx))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "split.bin"
            codeplug.save_copy(path)
            saved = path.read_bytes()
            expected = bytearray(original)
            expected[0x37B2:0x37B4] = b"\x00\x00"
            expected[0x37BB:0x37BD] = b"\x7E\xEF"
            self.assertEqual(saved, expected)
            reopened = Codeplug(saved)
            self.assertEqual(reopened.channel(1).fields, split)
            self.assertFalse(reopened.edit(1, split))
            self.assertTrue(reopened.edit(1, Fields(0x293, 4660, 123456)))
            path = Path(temp) / "follow.bin"
            reopened.save_copy(path)
            expected = bytearray(original)
            expected[0x37B2:0x37B4] = b"\xff\xff"
            self.assertEqual(path.read_bytes(), expected)
            self.assertIsNone(Codeplug(path.read_bytes()).channel(1).fields.tx_nac)


if __name__ == "__main__":
    unittest.main()
