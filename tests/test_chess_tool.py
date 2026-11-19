import json
import unittest

from lantern.chess_tool import analyze_position


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
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                analyze_position(arguments)
