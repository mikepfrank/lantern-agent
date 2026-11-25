import json
import unittest
from unittest.mock import patch

from lantern.chess_tool import ChessEngineError, ChessInputError, analyze_position


class ChessToolTests(unittest.TestCase):
    def test_unique_mate_in_one(self):
        result = analyze_position({"fen": "7k/8/5KQ1/8/8/8/8/8 w - - 0 1"})
        line = result["candidates"][0]
        self.assertEqual(line["san"], ["Qg7#"])
        self.assertEqual(line["uci"], ["g6g7"])
        self.assertEqual(line["mate_in_plies"], 1)
        self.assertIsNone(line["score_cp"])
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_finished_position(self):
        result = analyze_position({"fen": "7k/6Q1/5K2/8/8/8/8/8 b - - 1 1"})
        self.assertEqual(result["terminal"]["reason"], "checkmate")
        self.assertEqual(result["candidates"], [])

    def test_bad_arguments(self):
        for arguments in (None, {}, {"fen": 42}, {"fen": "invalid"}, {"fen": "X"*257},
                          {"fen": "7k/8/5KQ1/8/8/8/8/8 w - - 0 1", "depth": 80}):
            with self.subTest(arguments=arguments), self.assertRaises(ChessInputError):
                analyze_position(arguments)

    def test_report_rank_width_field_count_and_excess_material(self):
        for fen, diagnostic in (
            ("4k3/8/8/8/8/8/8/R2QR2K1 w - - 0 1",
             r"rank 1 \('R2QR2K1'\) expands to 9"),
            ("6k3/8/8/8/8/8/8/K7 b - - 0 1", r"rank 8 \('6k3'\) expands to 10"),
            ("6k/8/8/8/8/8/8/K7 b - - 0 1", r"rank 8 \('6k'\) expands to 7"),
            ("6k1/8/8/8/8/8/K7 b - - 0 1", r"7 ranks.*Expected 8"),
            ("6k1/8/8/8/8/8/8/K7 w - 4 12", r"5 fields.*castling, en passant"),
            ("rnbqkbnr/pppppppp/p7/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
             r"Black 9 pawns and 17 pieces"),
        ):
            with self.subTest(fen=fen), self.assertRaisesRegex(ChessInputError, diagnostic):
                analyze_position({"fen": fen})

    def test_canonical_echo_preserves_the_submitted_board(self):
        placement = "7k/8/5KQ1/8/8/8/P14BP/8"
        result = analyze_position({"fen": placement + " w - - 0 1"})
        self.assertEqual(result["fen"], placement.replace("P14BP", "P5BP") + " w - - 0 1")

    def test_internal_failure_has_a_separate_error_type(self):
        with patch("lantern.chess_tool.analyze", side_effect=ValueError("synthetic engine fault")):
            with self.assertRaises(ChessEngineError) as caught:
                analyze_position({"fen": "7k/8/5KQ1/8/8/8/8/8 w - - 0 1"})
        self.assertNotIn("synthetic engine fault", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, ValueError)
