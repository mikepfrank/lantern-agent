"""Model-facing position analysis using the original Astra chess workbench.

The sibling astra_engine package contains the rules, search, and search
diagnostics from mikepfrank/astra-chess at commit
ceb65169f03e99282dd40b3ee0e277c0e8a2947e, with local FEN input diagnostics.
"""

from .astra_engine.rules import FENError, Position
from .astra_engine.search import analyze


CHESS_TOOL = {
    "name": "chess_analyze",
    "description": (
        "Analyze one orthodox chess position with Astra's local tactical engine. "
        "Supply a six-field FEN. The returned fen is the normalized input position. "
        "Candidate lines use SAN and UCI, with scores "
        "from the side to move. A positive mate_in_plies counts half-moves to "
        "mate for that side. Earlier repetitions cannot be recovered from FEN."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"fen": {
            "type": "string", "maxLength": 256,
            "description": "Board (8 ranks of 8 squares), side to move, castling, en passant, halfmove clock, fullmove number.",
        }},
        "required": ["fen"],
        "additionalProperties": False,
    },
}


class ChessInputError(ValueError):
    """A chess argument diagnostic suitable for returning to Lantern."""


class ChessEngineError(RuntimeError):
    """Internal analysis failure. The exception cause is for host diagnostics."""


def analyze_position(arguments: dict) -> dict:
    """Return a bounded search with ranked lines, draw claims, and terminal status."""
    if not isinstance(arguments, dict) or set(arguments) != {"fen"}:
        raise ChessInputError("chess_analyze requires one chess argument named 'fen'. "
                              "Supply the six-field position string. Search settings are fixed.")
    fen = arguments["fen"]
    if not isinstance(fen, str) or len(fen) > 256:
        raise ChessInputError("The 'fen' argument must be a string of at most 256 characters.")
    try:
        return _analysis_result(fen)
    except ChessInputError:
        raise
    except Exception as exc:
        raise ChessEngineError("The chess engine could not finish this analysis due to an internal error.") from exc


def _analysis_result(fen: str) -> dict:
    try:
        position = Position.from_fen(fen)
    except FENError as exc:
        raise ChessInputError(str(exc)) from exc
    analysis = analyze(position, max_depth=3, time_limit=2.0, multipv=3)
    return {
        "fen": analysis["start_fen"],
        "turn": analysis["turn"],
        "completed_depth": analysis["completed_depth"],
        "timed_out": analysis["timed_out"],
        "unevaluated_fallback": analysis["fallback"],
        "terminal": analysis["terminal"],
        "recommended_action": analysis["recommended_action"],
        "draw_claim_reasons": analysis["draw_claim_reasons"],
        "draw_claim_by_intended_moves": analysis["draw_claim_by_intended_moves"],
        "candidates": [
            {
                "uci": line["uci"],
                "san": line["san"],
                "mate_in_plies": line["mate_in_plies"],
                "score_cp": line["score_cp"] if line["mate_in_plies"] is None else None,
            }
            for line in analysis["candidates"]
        ],
    }
