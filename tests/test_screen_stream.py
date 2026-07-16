import unittest

from autoperf.screen_stream import AccessUnitAssembler, AnnexBSplitter

SPS = b"\x67\x64\x00\x34\xaa"
PPS = b"\x68\xee\x3c\x80"
IDR = b"\x65\x88\x84" + b"\xab" * 10
DELTA = b"\x41\x9a" + b"\xcd" * 5
SEI = b"\x06\x01\x02"


def annexb(*nals: bytes, start_code: bytes = b"\x00\x00\x00\x01") -> bytes:
    return b"".join(start_code + nal for nal in nals)


class AnnexBSplitterTests(unittest.TestCase):
    def test_splits_a_chunk_into_nals_bounded_by_a_following_start_code(self):
        # A trailing start code with no payload after it is what confirms DELTA's
        # end -- without it, DELTA (the last NAL) would still be buffered, since
        # its length can't be known until the next start code is seen.
        splitter = AnnexBSplitter()
        nals = splitter.feed(annexb(SPS, PPS, IDR, DELTA) + b"\x00\x00\x00\x01")
        self.assertEqual([n[0] for n in nals], [7, 8, 5, 1])
        self.assertEqual(nals[0][1], SPS)
        self.assertEqual(nals[2][1], IDR)

    def test_supports_three_byte_start_codes(self):
        splitter = AnnexBSplitter()
        nals = splitter.feed(annexb(SPS, PPS, start_code=b"\x00\x00\x01") + b"\x00\x00\x01")
        self.assertEqual([n[0] for n in nals], [7, 8])

    def test_the_last_nal_in_a_chunk_is_held_until_its_end_is_confirmed(self):
        # With nothing after PPS yet, only SPS (bounded by PPS's start code) can
        # be emitted -- PPS itself might still be growing.
        splitter = AnnexBSplitter()
        nals = splitter.feed(annexb(SPS, PPS))
        self.assertEqual([n[0] for n in nals], [7])

    def test_holds_incomplete_trailing_nal_until_more_data_arrives(self):
        splitter = AnnexBSplitter()
        stream = annexb(SPS, PPS, IDR)
        first_nals = splitter.feed(stream[:-3])  # cut off the tail of IDR
        self.assertEqual([n[0] for n in first_nals], [7, 8])  # IDR not yet bounded
        second_nals = splitter.feed(stream[-3:] + annexb(DELTA) + b"\x00\x00\x00\x01")
        self.assertEqual([n[0] for n in second_nals], [5, 1])

    def test_handles_start_code_split_across_chunks(self):
        stream = annexb(SPS, PPS)
        split_point = len(b"\x00\x00\x00\x01" + SPS) + 2  # inside the second start code
        splitter = AnnexBSplitter()
        first_nals = splitter.feed(stream[:split_point])
        self.assertEqual(first_nals, [])  # the split start code can't be confirmed yet
        second_nals = splitter.feed(stream[split_point:] + annexb(IDR) + b"\x00\x00\x00\x01")
        self.assertEqual([n[0] for n in second_nals], [7, 8, 5])

    def test_feed_returns_empty_list_when_no_complete_nal_yet(self):
        splitter = AnnexBSplitter()
        self.assertEqual(splitter.feed(b"\x00\x00\x00\x01" + SPS[:2]), [])


class AccessUnitAssemblerTests(unittest.TestCase):
    def test_sps_and_pps_are_buffered_until_idr_arrives(self):
        assembler = AccessUnitAssembler()
        self.assertIsNone(assembler.feed(7, SPS))
        self.assertIsNone(assembler.feed(8, PPS))
        is_key, framed = assembler.feed(5, IDR)
        self.assertTrue(is_key)
        self.assertEqual(framed, b"\x00\x00\x00\x01" + SPS + b"\x00\x00\x00\x01" + PPS + b"\x00\x00\x00\x01" + IDR)

    def test_delta_nal_is_its_own_access_unit(self):
        assembler = AccessUnitAssembler()
        is_key, framed = assembler.feed(1, DELTA)
        self.assertFalse(is_key)
        self.assertEqual(framed, b"\x00\x00\x00\x01" + DELTA)

    def test_unrelated_nal_types_are_dropped(self):
        assembler = AccessUnitAssembler()
        self.assertIsNone(assembler.feed(6, SEI))  # SEI
        self.assertIsNone(assembler.feed(9, b"\xf0"))  # AUD

    def test_pending_params_are_cleared_after_a_key_unit(self):
        assembler = AccessUnitAssembler()
        assembler.feed(7, SPS)
        assembler.feed(8, PPS)
        assembler.feed(5, IDR)
        # A second IDR with no new SPS/PPS before it should not re-bundle the old ones.
        is_key, framed = assembler.feed(5, IDR)
        self.assertTrue(is_key)
        self.assertEqual(framed, b"\x00\x00\x00\x01" + IDR)


if __name__ == "__main__":
    unittest.main()
