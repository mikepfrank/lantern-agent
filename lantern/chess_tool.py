"""Model-facing position analysis using the original Astra chess workbench.

The sibling astra_engine package contains the rules, search, and search
diagnostics from mikepfrank/astra-chess at commit
ceb65169f03e99282dd40b3ee0e277c0e8a2947e, copied without changes.
"""

from .astra_engine.rules import Position
from .astra_engine.search import analyze


CHESS_TOOL = {
    "name": "chess_analyze",
    "description": (
        "Analyze one orthodox chess position with Astra's local tactical engine. "
        "Supply a six-field FEN. Candidate lines use SAN and UCI, with scores "
        "from the side to move. A positive mate_in_plies counts half-moves to "
        "mate for that side. Earlier repetitions cannot be recovered from FEN."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"fen": {"type": "string", "description": "Six-field FEN."}},
        "required": ["fen"],
        "additionalProperties": False,
    },
}


def analyze_position(arguments: dict) -> dict:
    """Return a bounded search with ranked lines, draw claims, and terminal status."""
    if not isinstance(arguments, dict) or set(arguments) != {"fen"}:
        raise ValueError("chess_analyze requires exactly one argument named fen")
    fen = arguments["fen"]
    if not isinstance(fen, str) or len(fen) > 256:
        raise ValueError("fen must be a string of at most 256 characters")
    analysis = analyze(Position.from_fen(fen), max_depth=3, time_limit=2.0, multipv=3)
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
