import os
import shutil
import threading
import math
import time
import random
from pathlib import Path
from flask import Flask, render_template, request, jsonify
from stockfish import Stockfish
import chess
import platform
import os
import stat

app = Flask(__name__)

# Locate Stockfish binary across different environments (Local Windows, Docker Linux, Cloud)
def find_stockfish():
    env_path = os.environ.get("STOCKFISH_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    candidate_paths = [
        r"C:\Users\alpaa\Downloads\stockfish-windows-x86-64-universal\stockfish\stockfish-windows-x86-64-universal.exe",
        "stockfish.exe",
        "./stockfish.exe",
        "./stockfish",
        "/usr/games/stockfish",
        "/usr/bin/stockfish",
        "/usr/local/bin/stockfish",
    ]
    for p in candidate_paths:
        if os.path.exists(p):
            if not p.endswith(".exe") and platform.system() != "Windows":
                try:
                    st = os.stat(p)
                    os.chmod(p, st.st_mode | stat.S_IEXEC)
                except Exception:
                    pass
            return p

    which_path = shutil.which("stockfish") or shutil.which("stockfish.exe")
    if which_path and os.path.exists(which_path):
        return which_path

    raise FileNotFoundError("Stockfish executable not found. Please install Stockfish or set STOCKFISH_PATH.")

STOCKFISH_19_PATH = find_stockfish()
print(f"[*] Initializing Stockfish strictly from: {STOCKFISH_19_PATH}")
stockfish = Stockfish(path=STOCKFISH_19_PATH, depth=15)

# Threading Lock to prevent concurrency/race conditions across multiple Flask threads
engine_lock = threading.Lock()

# Safe engine configuration using get_engine_parameters()
current_params = stockfish.get_engine_parameters()
desired_config = {
    "Threads": min(4, os.cpu_count() or 2),
    "Hash": 32,
    "Skill Level": 20,
    "Minimum Thinking Time": 20,
}
safe_config = {k: v for k, v in desired_config.items() if k in current_params}
stockfish.update_engine_parameters(safe_config)
print(f"[*] Stockfish 19 configured safely with parameters: {stockfish.get_engine_parameters()}")


def eval_to_centipawns(eval_obj: dict) -> int:
    """
    Converts Stockfish evaluation object into integer centipawns.
    Mate in N is scaled to 10000 - N*100 for smooth math.
    """
    if not eval_obj:
        return 0
    t = eval_obj.get("type", "cp")
    v = eval_obj.get("value", 0)
    if t == "cp":
        return int(v)
    elif t == "mate":
        if v > 0:
            return 10000 - v * 100
        elif v < 0:
            return -10000 + abs(v) * 100
        else:
            return -10000
    return 0


def build_board_with_history(fen: str | None, moves: list | None = None, initial_fen: str | None = None) -> chess.Board:
    """
    Constructs a chess.Board with full move history so python-chess can accurately
    detect threefold repetitions (board.is_repetition(3)), fifty-move rules, etc.
    """
    if moves:
        try:
            start_fen = initial_fen or chess.STARTING_FEN
            board = chess.Board(start_fen)
            for m in moves:
                if isinstance(m, dict):
                    uci_str = m.get("uci") or m.get("move")
                    san_str = m.get("san")
                    if uci_str:
                        board.push_uci(uci_str)
                    elif san_str:
                        board.push_san(san_str)
                elif isinstance(m, str):
                    try:
                        board.push_uci(m)
                    except Exception:
                        board.push_san(m)
            return board
        except Exception:
            pass

    if fen:
        try:
            return chess.Board(fen)
        except Exception:
            pass
    return chess.Board()


def move_score_to_cp(m: dict) -> int:
    """
    Converts a candidate line score into an integer centipawn value.
    """
    if not m:
        return 0
    cp = m.get("Centipawn")
    mate = m.get("Mate")
    val = m.get("val")
    t = m.get("type", "cp")
    if t == "cp" or cp is not None:
        return int(cp if cp is not None else (val or 0))
    elif t == "mate" or mate is not None:
        mv = int(mate if mate is not None else (val or 0))
        return (10000 - mv * 100) if mv > 0 else (-10000 + abs(mv) * 100)
    return 0


def classify_candidate_loss(loss: int) -> str:
    """
    Classifies a candidate line based on its centipawn loss compared to Line #1:
    - Best (< 50 cp loss)
    - Inaccuracy (50-99 cp loss)
    - Mistake (100-199 cp loss)
    - Blunder (>= 200 cp loss)
    """
    if loss >= 200:
        return "Blunder"
    elif loss >= 100:
        return "Mistake"
    elif loss >= 50:
        return "Inaccuracy"
    else:
        return "Best"


def format_evaluation(eval_data: dict, turn: str) -> dict:
    """
    Normalizes Stockfish evaluation to standard White-perspective:
    (+ for White advantage, - for Black advantage)
    """
    eval_type = eval_data.get("type", "cp")
    raw_val = eval_data.get("value", 0)

    if eval_type == "cp":
        white_val = raw_val if turn == "w" else -raw_val
        pawns = round(white_val / 100.0, 2)
        formatted = f"+{pawns:.2f}" if pawns > 0 else f"{pawns:.2f}"
        return {
            "type": "cp",
            "side_value": raw_val,
            "white_value": white_val,
            "pawns": pawns,
            "formatted": formatted,
        }
    elif eval_type == "mate":
        white_val = raw_val if turn == "w" else -raw_val
        if white_val == 0:
            formatted = "Checkmate"
        elif white_val > 0:
            formatted = f"+M{white_val}"
        else:
            formatted = f"-M{abs(white_val)}"

        return {
            "type": "mate",
            "side_value": raw_val,
            "white_value": white_val,
            "mate_in": white_val,
            "formatted": formatted,
        }

    return {
        "type": "unknown",
        "side_value": 0,
        "white_value": 0,
        "pawns": 0.0,
        "formatted": "0.00",
    }


def check_is_sacrifice(prev_fen: str, curr_fen: str, last_move: dict) -> bool:
    """
    Detects whether a move involved sacrificing material (minor piece, rook, or queen)
    in exchange for tactical/positional advantage.
    """
    try:
        if not prev_fen or not last_move:
            return False
        uci = last_move.get("uci", "")
        if len(uci) < 4:
            return False

        prev_board = chess.Board(prev_fen)
        move = chess.Move.from_uci(uci)
        moving_piece = prev_board.piece_at(move.from_square)
        captured_piece = prev_board.piece_at(move.to_square)
        if not moving_piece:
            return False

        piece_vals = {
            chess.PAWN: 1,
            chess.KNIGHT: 3,
            chess.BISHOP: 3,
            chess.ROOK: 5,
            chess.QUEEN: 9,
            chess.KING: 100,
        }
        val_moving = piece_vals.get(moving_piece.piece_type, 0)
        val_captured = piece_vals.get(captured_piece.piece_type, 0) if captured_piece else 0

        curr_board = chess.Board(curr_fen)
        opp_color = not moving_piece.color

        # 1. Exchange sacrifice: Rook traded for minor piece or pawn
        if moving_piece.piece_type == chess.ROOK and val_captured in [1, 3]:
            return True

        # 2. Minor piece or Queen sacrifice
        if val_moving >= 3:
            attackers = curr_board.attackers(opp_color, move.to_square)
            defenders = curr_board.attackers(moving_piece.color, move.to_square)
            # Attacked by lesser value piece or pawn
            if attackers:
                for sq in attackers:
                    att_piece = curr_board.piece_at(sq)
                    if att_piece and piece_vals.get(att_piece.piece_type, 0) < val_moving:
                        return True
                # Hanging piece without defenders
                if not defenders and val_captured < val_moving:
                    return True

        return False
    except Exception:
        return False


def is_known_opening_theory(uci: str, san: str, fullmove: int) -> bool:
    """
    Identifies common theoretical book moves within the first 6 moves.
    """
    if fullmove > 6:
        return False
    book_moves = {
        "e2e4", "d2d4", "c2c4", "g1f3", "g2g3", "b2b3", "f2f4", "b1c3",
        "f1c4", "f1b5", "c1f4", "c1g5", "e1g1", "e1c1", "d2d3", "e2e3",
        "c7c5", "e7e5", "e7e6", "c7c6", "d7d5", "g8f6", "b8c6", "d7d6",
        "g7g6", "b7b6", "f8e7", "f8c5", "f8b4", "c8d7", "c8e6", "c8g4",
        "e8g8", "e8c8", "d5c4", "d5e4", "e5d4", "c5d4", "f3d4", "f6d5",
        "c4d5", "e4d5", "d4d5", "c6d4",
    }
    return uci in book_moves


def classify_move(
    uci_move: str,
    san_move: str,
    fullmove_number: int,
    cp_loss: int,
    is_top_move: bool,
    only_move_gap: int,
    is_piece_sac: bool,
    eval_before: int,
    eval_after: int,
    is_checkmate: bool,
) -> dict:
    """
    Visual Move Classification System (Chess.com Game Review style):
    - Book
    - Brilliant (!!)
    - Great (!)
    - Good / Best
    - Inaccuracy (?!)
    - Mistake (?)
    - Blunder (??)
    - Miss
    """
    is_book = is_known_opening_theory(uci_move, san_move, fullmove_number)

    # 1. Book (Standard opening theory within first few moves)
    if is_book and fullmove_number <= 6 and cp_loss <= 40:
        return {
            "name": "Book",
            "symbol": "Book",
            "glyph": "book",
            "badge_class": "badge-book",
            "color": "#94a3b8",
            "bg": "rgba(148, 163, 184, 0.16)",
            "border": "rgba(148, 163, 184, 0.4)",
            "description": "Standard opening theory",
            "cp_loss": cp_loss,
        }

    # 2. Miss (Failing to find a clear winning tactic or mate)
    if (eval_before >= 250 or eval_before >= 8000) and (cp_loss >= 120 or eval_after < 100):
        return {
            "name": "Miss",
            "symbol": "Miss",
            "glyph": "miss",
            "badge_class": "badge-miss",
            "color": "#f43f5e",
            "bg": "rgba(244, 63, 94, 0.22)",
            "border": "rgba(244, 63, 94, 0.55)",
            "description": "Failing to find a clear winning tactic",
            "cp_loss": cp_loss,
        }

    # 3. Blunder (??) (Massive evaluation drop, e.g. > 200 cp)
    if cp_loss >= 200 or (eval_before >= -50 and eval_after <= -200):
        return {
            "name": "Blunder",
            "symbol": "??",
            "glyph": "blunder",
            "badge_class": "badge-blunder",
            "color": "#ef4444",
            "bg": "rgba(239, 68, 68, 0.22)",
            "border": "rgba(239, 68, 68, 0.55)",
            "description": "Massive evaluation drop (> 200 centipawns)",
            "cp_loss": cp_loss,
        }

    # 4. Mistake (?) (Moderate evaluation drop, e.g. > 100 cp)
    if cp_loss >= 100:
        return {
            "name": "Mistake",
            "symbol": "?",
            "glyph": "mistake",
            "badge_class": "badge-mistake",
            "color": "#f97316",
            "bg": "rgba(249, 115, 22, 0.2)",
            "border": "rgba(249, 115, 22, 0.5)",
            "description": "Moderate evaluation drop (> 100 centipawns)",
            "cp_loss": cp_loss,
        }

    # 5. Inaccuracy (?!) (Minor evaluation drop, e.g. > 50 cp)
    if cp_loss >= 50:
        return {
            "name": "Inaccuracy",
            "symbol": "?!",
            "glyph": "inaccuracy",
            "badge_class": "badge-inaccuracy",
            "color": "#f59e0b",
            "bg": "rgba(245, 158, 11, 0.2)",
            "border": "rgba(245, 158, 11, 0.5)",
            "description": "Minor evaluation drop (> 50 centipawns)",
            "cp_loss": cp_loss,
        }

    # 6. Brilliant (!!) (Top engine move involving a sacrifice or massive gain)
    if (is_top_move or cp_loss <= 20) and is_piece_sac and (eval_after >= 60 or is_checkmate):
        return {
            "name": "Brilliant",
            "symbol": "!!",
            "glyph": "brilliant",
            "badge_class": "badge-brilliant",
            "color": "#06b6d4",
            "bg": "rgba(6, 182, 212, 0.22)",
            "border": "rgba(6, 182, 212, 0.55)",
            "description": "Top engine move involving a sacrifice or massive gain",
            "cp_loss": cp_loss,
        }

    # 7. Great (!) (The only good move in a complex position)
    if (is_top_move or cp_loss <= 15) and only_move_gap >= 120 and eval_after >= -80:
        return {
            "name": "Great",
            "symbol": "!",
            "glyph": "great",
            "badge_class": "badge-great",
            "color": "#0ea5e9",
            "bg": "rgba(14, 165, 233, 0.2)",
            "border": "rgba(14, 165, 233, 0.5)",
            "description": "The only good move in a complex position",
            "cp_loss": cp_loss,
        }

    # 8. Good (Solid, safe top-3 engine move)
    is_best = is_top_move or cp_loss <= 15
    return {
        "name": "Best" if is_best else "Good",
        "symbol": "Best" if is_best else "Good",
        "glyph": "best" if is_best else "good",
        "badge_class": "badge-good",
        "color": "#10b981",
        "bg": "rgba(16, 185, 129, 0.2)",
        "border": "rgba(16, 185, 129, 0.5)",
        "description": "Solid, safe top-tier engine move",
        "cp_loss": cp_loss,
    }


def generate_ai_coach_commentary(
    curr_fen: str,
    prev_fen: str | None,
    last_move: dict | None,
    eval_info: dict,
    best_move: str | None,
    top_moves: list,
    classification: dict | None = None
) -> dict:
    """
    Generates structured AI Coach commentary for every move, integrating
    the visual move classification and centipawn loss.
    """
    curr_board = chess.Board(curr_fen)

    # 1. Starting Position / No Move Played
    if not last_move:
        default_class = {
            "name": "Book",
            "symbol": "Book",
            "glyph": "book",
            "badge_class": "badge-book",
            "color": "#94a3b8",
            "bg": "rgba(148, 163, 184, 0.16)",
            "border": "rgba(148, 163, 184, 0.4)",
            "description": "Standard starting position",
            "cp_loss": 0,
        }
        return {
            "move_label": "Starting Position",
            "title": "Initial Setup",
            "category": "Opening Strategy",
            "verdict": "Ready to Play",
            "verdict_type": "neutral",
            "explanation": "Standard starting position with all pieces fully deployed. White holds first-move initiative. Classical chess strategy dictates fighting for central control, developing knights and bishops, and securing king safety via castling.",
            "tip": "Recommended opening moves: 1. e4 (King's Pawn), 1. d4 (Queen's Pawn), 1. c4 (English), or 1. Nf3 (Reti).",
            "classification": default_class,
        }

    uci = last_move.get("uci", "")
    san = last_move.get("san", uci)
    piece = last_move.get("piece", "").lower()
    color_char = last_move.get("color", "w")
    side = "White" if color_char == "w" else "Black"
    opponent_side = "Black" if color_char == "w" else "White"
    from_sq = last_move.get("from", "")
    to_sq = last_move.get("to", "")
    captured = last_move.get("captured")
    promotion = last_move.get("promotion")

    move_label = f"{san} ({side})"

    # 2. Check Game Endings & Direct Checks
    if curr_board.is_checkmate():
        class_obj = classification or {
            "name": "Brilliant",
            "symbol": "!!",
            "glyph": "!!",
            "badge_class": "badge-brilliant",
            "color": "#06b6d4",
            "bg": "rgba(6, 182, 212, 0.22)",
            "border": "rgba(6, 182, 212, 0.55)",
            "description": "Game-ending checkmate",
            "cp_loss": 0,
        }
        return {
            "move_label": move_label,
            "title": f"Checkmate! {side} Wins",
            "category": "Decisive Tactic",
            "verdict": "Checkmate",
            "verdict_type": "best",
            "explanation": f"{side} delivers a fatal checkmate with {san}! The {opponent_side.lower()} king is trapped with no legal evasion, blocking move, or capturing response available.",
            "tip": "Outstanding tactical finish! Game concluded.",
            "classification": class_obj,
        }

    if curr_board.is_stalemate():
        return {
            "move_label": move_label,
            "title": "Stalemate - Draw",
            "category": "Endgame Tactic",
            "verdict": "Draw by Stalemate",
            "verdict_type": "neutral",
            "explanation": f"{san} leaves the {opponent_side.lower()} king without any legal moves while not being in check. Under standard rules, this results in an immediate draw.",
            "tip": "Always verify opponent escape squares before making a finishing squeeze.",
            "classification": classification or {},
        }

    is_check = curr_board.is_check()

    # Classification details
    class_name = classification.get("name", "Good") if classification else "Good"
    class_symbol = classification.get("symbol", "✓") if classification else "✓"
    cp_loss = classification.get("cp_loss", 0) if classification else 0

    verdict = f"{class_name} ({class_symbol})"
    verdict_type = "best" if class_name in ["Brilliant", "Great", "Best"] else \
                   "good" if class_name in ["Good", "Book"] else \
                   "inaccuracy" if class_name == "Inaccuracy" else "blunder"

    eval_score = eval_info.get("formatted", "0.00")

    # 3. Opening Book Recognition & Move Catalogs
    openings_map = {
        "e2e4": ("King's Pawn Opening", "Central Control", "White stakes an immediate claim in the center, seizing control of d5 and f5 while clearing diagonal pathways for both the light-squared bishop and queen.", "Develop minor pieces toward the center (Nf3, Bc4) and prepare kingside castling."),
        "d2d4": ("Queen's Pawn Opening", "Central Control", "White establishes a solid central pawn on d4, controlling e5 and c5 with built-in protection from the queen.", "Look to support d4 with Nf3, c4 (Queen's Gambit), or Nc3."),
        "c2c4": ("English Opening", "Flank Control", "White controls the crucial d5 square from the flank without committing central pawns early, allowing flexible setups.", "Follow up with g3/Bg2 (fianchetto) or Nc3 to increase pressure on d5."),
        "g1f3": ("Reti / King's Indian Setup", "Flexible Development", "Develops the knight toward the center, prevents early ...e5 from Black, and prepares rapid kingside castling.", "Can transition into English, Catalan, or King's Indian Attack."),
        "c7c5": ("Sicilian Defense", "Asymmetric Counter", "Black aggressively fights for the central d4 square with a flank pawn, leading to sharp, dynamic, unbalanced battles.", "Prepare ...d6 or ...Nc6 and contest the half-open c-file."),
        "e7e5": ("Open Game", "Direct Equality", "Black establishes an equal central foothold on e5, blunting White's expansion and opening lines for the bishop and queen.", "Defend e5 or develop knights toward the center (...Nc6, ...Nf6)."),
        "e7e6": ("French Defense", "Pawn Chain Counter", "Black prepares a solid ...d5 counter-thrust against White's center while maintaining a resilient, fortress-like pawn structure.", "Follow up with ...d5 to challenge White's e4 pawn directly."),
        "c7c6": ("Caro-Kann Defense", "Solid Pawn Setup", "Black prepares ...d5 while keeping the light-squared bishop's diagonal unobstructed, avoiding French Defense weaknesses.", "Strike the center with ...d5 on the next move."),
        "d7d5": ("Scandinavian Defense", "Direct Challenge", "Black immediately challenges White's central e4 pawn, leading to early tactical line openings.", "If White captures (exd5), prepare queen or knight recapture."),
        "g8f6": ("Indian Defense Setup", "Hypermodern Flexibility", "Black stops White from playing e4 immediately while keeping open dynamic options like ...c5, ...e6, or ...g6.", "Prepare king safety and decide on central pawn break (...c5 or ...d5)."),
        "b1c3": ("Queen's Knight Development", "Piece Activation", "Develops the knight to c3, applying direct pressure to central d5 and e4 squares.", "Coordinate knight action with central pawn advances or bishop development."),
        "f1c4": ("Italian / Bishop Attack", "King Targeting", "Develops the bishop along the active a2-g1 diagonal, targeting the sensitive f7 square near the enemy king.", "Coordinate with Nf3 and castle kingside (O-O) quickly."),
        "f1b5": ("Spanish / Ruy Lopez Pin", "Indirect Pressure", "Develops the bishop to b5, pinning or pressuring the defending knight on c6 and challenging Black's central control.", "Maintain pressure on e5 and castle kingside."),
        "e1g1": ("Kingside Castle (O-O)", "King Safety & Activation", "Safely tucks the white king behind a protective wall of pawns and activates the rook onto the central f-file.", "Connect rooks and begin central or kingside operations."),
        "e8g8": ("Kingside Castle (O-O)", "King Safety & Activation", "Safely relocates the black king into the corner and activates the rook toward central lines.", "Prepare counterplay along central or flank files."),
        "e1c1": ("Queenside Castle (O-O-O)", "Aggressive King Safety", "Castles queenside, moving the king to the flank while immediately deploying the rook on the open or semi-open d-file.", "Prepare opposite-side castling pawn storms on the kingside."),
        "e8c8": ("Queenside Castle (O-O-O)", "Aggressive King Safety", "Castles queenside, placing the king away from center fire while coordinating rooks along the d-file.", "Watch for central tactical breaks."),
    }

    if uci in openings_map and (curr_board.fullmove_number <= 4 or "Castle" in openings_map[uci][0]):
        title, cat, expl, tip = openings_map[uci]
        loss_text = f" (Delta: -{cp_loss} cp)" if cp_loss > 0 else " (Preserves balance)"
        return {
            "move_label": move_label,
            "title": title,
            "category": cat,
            "verdict": verdict,
            "verdict_type": verdict_type,
            "explanation": f"{side} plays {san}. {expl} Current evaluation: {eval_score}{loss_text}.",
            "tip": tip,
            "classification": classification,
        }

    # 4. Tactical Actions: Castling, Captures, Promotions, Checks
    is_castling = last_move.get("is_castling") or (piece == "k" and abs(ord(from_sq[0]) - ord(to_sq[0])) > 1)
    if is_castling:
        castle_type = "kingside (O-O)" if to_sq[0] in ["g", "h"] else "queenside (O-O-O)"
        return {
            "move_label": move_label,
            "title": f"Castling {castle_type.split()[0].title()}",
            "category": "King Safety",
            "verdict": verdict,
            "verdict_type": verdict_type,
            "explanation": f"{side} castles {castle_type}, fulfilling a core positional imperative: tucking the king behind a protective pawn barrier while activating the rook into central play. Evaluation stands at {eval_score}.",
            "tip": "King safety achieved! Focus on coordinating your rooks and pursuing central pawn breaks.",
            "classification": classification,
        }

    if promotion:
        promoted_name = "Queen" if promotion.lower() == "q" else "Rook" if promotion.lower() == "r" else "Knight"
        return {
            "move_label": move_label,
            "title": f"Pawn Promoted to {promoted_name}!",
            "category": "Pawn Promotion",
            "verdict": verdict,
            "verdict_type": verdict_type,
            "explanation": f"{side} marches the passed pawn to the back rank ({to_sq}) and promotes to a {promoted_name}! This introduces overwhelming material power in the endgame. Position evaluation: {eval_score}.",
            "tip": "Use your new piece dominance to deliver checkmate or simplify remaining enemy material.",
            "classification": classification,
        }

    if captured:
        cap_names = {"q": "Queen", "r": "Rook", "b": "Bishop", "n": "Knight", "p": "Pawn"}
        cap_name = cap_names.get(str(captured).lower(), "Piece")
        piece_names = {"q": "Queen", "r": "Rook", "b": "Bishop", "n": "Knight", "p": "Pawn", "k": "King"}
        acting_piece = piece_names.get(piece, "Piece")

        check_text = " and delivers check to the enemy king!" if is_check else "."
        return {
            "move_label": move_label,
            "title": f"Tactical Capture: {san}",
            "category": "Material Exchange",
            "verdict": verdict,
            "verdict_type": verdict_type,
            "explanation": f"{side} executes a tactical capture on {to_sq}, eliminating the {opponent_side.lower()} {cap_name} with the {acting_piece}{check_text} This alters the balance of material. Engine evaluation: {eval_score}.",
            "tip": "Evaluate whether your opponent can immediately recapture or if you have seized a permanent material edge.",
            "classification": classification,
        }

    if is_check:
        return {
            "move_label": move_label,
            "title": f"{san} Delivers Check!",
            "category": "Tactical Attack",
            "verdict": verdict,
            "verdict_type": verdict_type,
            "explanation": f"{side} delivers a direct check to the {opponent_side.lower()} king with {san}! Forcing the opponent to respond immediately limits their tactical choices and secures attacking tempo. Current evaluation: {eval_score}.",
            "tip": "Look for follow-up forks, pins, or discovered attacks while the enemy king is on the run.",
            "classification": classification,
        }

    # 5. Positional Explanations by Piece Type
    if piece == "n":
        central_outposts = ["d5", "e5", "d4", "e4", "f5", "f4", "c5", "c4"]
        if to_sq in central_outposts:
            title = "Knight Centralization"
            category = "Outpost Dominance"
            explanation = f"{side} places the knight on the high-value central outpost {to_sq}! A centralized knight wields tremendous tactical reach, radiating control over key central squares deep in enemy territory."
            tip = "Support this knight with pawn anchors to make it virtually impossible to dislodge."
        else:
            title = "Knight Maneuver"
            category = "Piece Activation"
            explanation = f"{side} maneuvers the knight to {to_sq}, repositioning toward more active diagonals, defending friendly pawn structures, or clearing squares for back-rank pieces."
            tip = "Identify weak opponent squares or holes in the pawn chain where this knight can jump next."

    elif piece == "b":
        fianchetto_squares = ["g2", "b2", "g7", "b7"]
        if to_sq in fianchetto_squares:
            title = "Bishop Fianchetto"
            category = "Long Diagonal Control"
            explanation = f"{side} fianchettos the bishop on {to_sq}, directing long-range sniper fire down the great diagonal across the board."
            tip = "Keep the long diagonal clear of friendly pawns so your bishop exerts maximum board influence."
        elif to_sq in ["g5", "b5", "g4", "b4"]:
            title = "Pinning Bishop Development"
            category = "Tactical Pin / Pressure"
            explanation = f"{side} develops the bishop actively to {to_sq}, pinning or applying immediate pressure against the defending knight on the opponent's side."
            tip = "Watch for potential pawn kicks (like ...h6 or ...a6) and decide whether to maintain the pin or trade."
        else:
            title = "Bishop Activation"
            category = "Open Diagonal Play"
            explanation = f"{side} posts the bishop to an active diagonal ({to_sq}), exerting cross-board pressure and harmonizing with the surrounding pieces."
            tip = "Target the opponent's weaker pawn outposts or restrict enemy knight mobility."

    elif piece == "r":
        file_letter = to_sq[0]
        if to_sq[1] in ["7", "2"]:
            title = "Rook on the 7th Rank!"
            category = "Positional Infiltration"
            explanation = f"{side}'s rook invades the vital 7th rank ({to_sq})! In classical chess, a rook on the 7th restricts the opponent's king and attacks undefended base pawns."
            tip = "Consider doubling rooks along this rank to deliver overwhelming tactical pressure."
        elif file_letter in ["d", "e"]:
            title = "Central Rook Placement"
            category = "Central Control"
            explanation = f"{side} swings the rook to the central {file_letter}-file ({to_sq}), reinforcing central pawn levers and preparing for tactical line clearance."
            tip = "Anticipate pawn breaks in the center that will open this file into an attacking corridor."
        else:
            title = "Rook Mobilization"
            category = "File Occupation"
            explanation = f"{side} positions the rook on the {file_letter}-file ({to_sq}), seeking vertical activity and coordinating with the back rank."
            tip = "Work toward opening this file by trading off opposing pawns."

    elif piece == "q":
        title = "Queen Activity"
        category = "Queen Coordination"
        explanation = f"{side} mobilizes the queen to {to_sq}, projecting immense multi-directional force across files, ranks, and diagonals."
        tip = "Ensure the queen does not become an easy target for enemy minor piece attacks with tempo."

    elif piece == "k":
        if curr_board.fullmove_number >= 30 or len(curr_board.piece_map()) <= 12:
            title = "Endgame King Activation"
            category = "Endgame Principle"
            explanation = f"{side} advances the king to {to_sq}! In the endgame, the king transforms from a sheltered liability into an aggressive attacking piece essential for escorting pawns."
            tip = "March the king toward key central squares and support your passed pawns."
        else:
            title = "King Repositioning"
            category = "Defensive Adjustment"
            explanation = f"{side} steps the king to {to_sq}, stepping away from tactical alignment and seeking safety behind pawn shields."
            tip = "Keep the protective pawn shield intact around your relocated king."

    else:  # Pawn
        if to_sq in ["e4", "d4", "e5", "d5"]:
            title = "Central Pawn Advance"
            category = "Central Territory"
            explanation = f"{side} pushes a central pawn to {to_sq}, claiming dominant spatial territory and denying enemy knights comfortable staging squares."
            tip = "Reinforce this central anchor with adjacent pawn chains or minor pieces."
        elif to_sq in ["c4", "f4", "c5", "f5"]:
            title = "Flank Pawn Thrust"
            category = "Pawn Lever / Break"
            explanation = f"{side} strikes on the flank with {san}, preparing to challenge the enemy center or pry open attacking files."
            tip = "Assess potential file openings for your rooks once pawns clash."
        elif to_sq in ["a3", "h3", "a6", "h6"]:
            title = "Prophylactic Pawn Step"
            category = "Prophylaxis & King Air"
            explanation = f"{side} plays {san}, a prophylactic move that denies enemy pieces key outpost squares on the flank and creates valuable escape air (luft) for the king."
            tip = "Follow up by resuming active piece development."
        else:
            title = "Pawn Push"
            category = "Pawn Structure"
            explanation = f"{side} advances the pawn to {to_sq}, altering the board's pawn tension and opening pathways behind it."
            tip = "Keep in mind that pawns cannot move backward; ensure the squares left behind remain defended."

    return {
        "move_label": move_label,
        "title": title,
        "category": category,
        "verdict": verdict,
        "verdict_type": verdict_type,
        "explanation": f"{explanation} Evaluation: {eval_score}.",
        "tip": tip,
        "classification": classification,
    }


@app.route("/")
def index():
    """Serves the main chess analysis dashboard."""
    return render_template("index.html")


@app.route("/engine-info", methods=["GET"])
def engine_info():
    """Returns Stockfish 19 diagnostic info and active parameters."""
    with engine_lock:
        params = stockfish.get_engine_parameters()
        depth = stockfish.get_depth()

    return jsonify({
        "engine": "Stockfish 19",
        "path": STOCKFISH_19_PATH,
        "status": "online",
        "parameters": params,
        "depth": depth,
    })


def map_elo_to_depth_and_skill(target_elo: int) -> tuple[int, int]:
    """
    Dynamically maps target_elo to Stockfish Depth (2-15) and Skill Level (0-20)
    to simulate human chess player flaws across rating bands.
    e.g., Elo 1200 = Depth 5, Skill 4.
    """
    try:
        target_elo = int(target_elo)
    except Exception:
        target_elo = 1500

    target_elo = max(400, min(3200, target_elo))

    if target_elo <= 800:
        depth = 2
        skill = 1
    elif target_elo < 1000:
        depth = 3
        skill = 2
    elif target_elo < 1200:
        depth = 4
        skill = 3
    elif target_elo < 1400:  # Elo 1200 - 1399
        depth = 5
        skill = 4           # e.g., Elo 1200 = Depth 5, Skill 4
    elif target_elo < 1600:
        depth = 7
        skill = 7
    elif target_elo < 1800:
        depth = 9
        skill = 10
    elif target_elo < 2000:
        depth = 11
        skill = 13
    elif target_elo < 2400:
        depth = 13
        skill = 16
    elif target_elo < 3000:
        depth = 15
        skill = 19
    else:  # 3000+
        depth = 15
        skill = 20

    return depth, skill


def search_position_with_time(fen: str, searchtime_ms: int, num_moves: int = 3, max_depth: int | None = None):
    """
    Executes a MultiPV search in Stockfish for strictly searchtime_ms milliseconds.
    If max_depth is provided, caps the search at max_depth.
    Returns: (eval_data, top_moves, best_move, depth_reached)
    """
    stockfish.set_fen_position(fen)
    stockfish._set_option("MultiPV", num_moves)
    if max_depth is not None and max_depth > 0:
        stockfish._put(f"go depth {max_depth} movetime {searchtime_ms}")
    else:
        stockfish._put(f"go movetime {searchtime_ms}")
    lines = stockfish._get_sf_go_command_output(lambda: None)

    top_moves_dict = {}
    depth = 0
    best_move = None

    for line in lines:
        tokens = line.strip().split()
        if not tokens:
            continue
        if tokens[0] == "bestmove":
            best_move = tokens[1] if tokens[1] != "(none)" else None
            continue
        if tokens[0] == "info" and "multipv" in tokens and "score" in tokens and "pv" in tokens:
            try:
                mpv_idx = tokens.index("multipv")
                mpv = int(tokens[mpv_idx + 1])
                score_idx = tokens.index("score")
                score_type = tokens[score_idx + 1]  # "cp" or "mate"
                score_val = int(tokens[score_idx + 2])
                pv_idx = tokens.index("pv")
                move_uci = tokens[pv_idx + 1]
                if "depth" in tokens:
                    d_idx = tokens.index("depth")
                    depth = max(depth, int(tokens[d_idx + 1]))

                top_moves_dict[mpv] = {
                    "Move": move_uci,
                    "Centipawn": score_val if score_type == "cp" else None,
                    "Mate": score_val if score_type == "mate" else None,
                    "type": score_type,
                    "val": score_val,
                }
            except Exception:
                pass

    ordered_moves = [top_moves_dict[k] for k in sorted(top_moves_dict.keys())]

    if ordered_moves:
        eval_data = {
            "type": ordered_moves[0]["type"],
            "value": ordered_moves[0]["val"]
        }
    else:
        # Checkmate or Stalemate
        eval_data = stockfish.get_evaluation()

    return eval_data, ordered_moves, best_move, depth


@app.route("/evaluate", methods=["GET", "POST"])
def evaluate():
    """
    Evaluates a chess FEN position and provides move classification based on
    centipawn loss and tactical context (Chess.com Game Review style).
    Thread-safe execution strictly adhering to the requested search time limit.
    Optionally accepts target_elo (< 3000) for dynamic engine limitation (Elo simulation).
    State Safety: Uses engine_lock and try...finally to guarantee restoration to Max settings.
    """
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        raw_fen = data.get("fen")
        fen = raw_fen.strip() if isinstance(raw_fen, str) else ""
        raw_prev = data.get("prev_fen")
        prev_fen = raw_prev.strip() if isinstance(raw_prev, str) and raw_prev.strip() else None
        last_move = data.get("last_move")
        num_moves = int(data.get("num_moves", 3))
        think_time = float(data.get("think_time", 1.0))
        moves = data.get("moves") or data.get("history") or data.get("move_stack")
        initial_fen = data.get("initial_fen")
        target_elo = data.get("target_elo")
    else:
        raw_fen = request.args.get("fen")
        fen = raw_fen.strip() if isinstance(raw_fen, str) else ""
        raw_prev = request.args.get("prev_fen")
        prev_fen = raw_prev.strip() if isinstance(raw_prev, str) and raw_prev.strip() else None
        last_move = None
        num_moves = int(request.args.get("num_moves", 3))
        think_time = float(request.args.get("think_time", 1.0))
        moves = None
        initial_fen = None
        target_elo = request.args.get("target_elo")

    if not fen:
        return jsonify({"success": False, "error": "Missing 'fen' parameter"}), 400

    parsed_target_elo = None
    if target_elo is not None and str(target_elo).strip() != "":
        try:
            parsed_target_elo = int(target_elo)
        except (ValueError, TypeError):
            parsed_target_elo = None

    # Strictly enforce search time limit between 0.5s and 10s (default 1s)
    think_time = max(0.5, min(10.0, think_time))
    searchtime_ms = int(think_time * 1000)

    with engine_lock:
        if not stockfish.is_fen_valid(fen):
            return jsonify({"success": False, "error": f"Invalid FEN string: '{fen}'"}), 400

        try:
            active_depth = 15
            active_skill = 20
            limit_depth = None

            # Dynamic Engine Limitation: If target_elo is provided and < 3000
            if parsed_target_elo is not None and parsed_target_elo < 3000:
                active_depth, active_skill = map_elo_to_depth_and_skill(parsed_target_elo)
                limit_depth = active_depth
                stockfish.update_engine_parameters({"UCI_LimitStrength": False, "Skill Level": active_skill})
                stockfish.set_skill_level(active_skill)
                stockfish.set_depth(active_depth)
            else:
                stockfish.update_engine_parameters({"UCI_LimitStrength": False, "Skill Level": 20})
                stockfish.set_skill_level(20)
                stockfish.set_depth(15)

            fen_parts = fen.split()
            turn = fen_parts[1] if len(fen_parts) > 1 else "w"

            # Reconstruct board with full move history to detect repetitions
            board = build_board_with_history(fen, moves=moves, initial_fen=initial_fen)
            is_checkmate = board.is_checkmate()
            is_stalemate = board.is_stalemate()
            is_repetition = board.is_repetition(3)
            can_claim_draw = board.can_claim_threefold_repetition()
            is_fifty_moves = board.is_fifty_moves()
            is_draw = is_stalemate or is_repetition or can_claim_draw or is_fifty_moves
            fullmove = board.fullmove_number

            # 1. Evaluate previous FEN if provided (quick search to calculate centipawn loss and candidate moves)
            prev_eval = None
            prev_top = []
            is_top_move = False
            only_move_gap = 0
            eval_before = 0

            if prev_fen and stockfish.is_fen_valid(prev_fen):
                prev_ms = min(250, searchtime_ms)
                prev_eval, prev_top, best_prev, _ = search_position_with_time(
                    prev_fen, prev_ms, num_moves=2, max_depth=limit_depth
                )
                eval_before = eval_to_centipawns(prev_eval)

                if last_move and prev_top:
                    best_prev = prev_top[0].get("Move")
                    is_top_move = (last_move.get("uci") == best_prev)

                    if len(prev_top) >= 2:
                        sc1 = eval_to_centipawns({"type": "cp" if prev_top[0].get("Centipawn") is not None else "mate", "value": prev_top[0].get("Centipawn") or prev_top[0].get("Mate") or 0})
                        sc2 = eval_to_centipawns({"type": "cp" if prev_top[1].get("Centipawn") is not None else "mate", "value": prev_top[1].get("Centipawn") or prev_top[1].get("Mate") or 0})
                        only_move_gap = sc1 - sc2

            # 2. Evaluate current FEN strictly for searchtime_ms, respecting dynamic depth limitation
            curr_eval, raw_top_moves, best_move, engine_depth = search_position_with_time(
                fen, searchtime_ms, num_moves=num_moves, max_depth=limit_depth
            )
            formatted_eval = format_evaluation(curr_eval, turn)

            # 3. Calculate Centipawn Loss from moving player's perspective
            eval_after = -eval_to_centipawns(curr_eval)

            if is_top_move:
                cp_loss = 0
            else:
                cp_loss = max(0, eval_before - eval_after)

            # Format candidate moves and calculate centipawn loss for lines #2 and #3 vs line #1
            formatted_moves = []
            line1_score = move_score_to_cp(raw_top_moves[0]) if raw_top_moves else 0

            for idx, m in enumerate(raw_top_moves):
                uci_move = m.get("Move")
                cp = m.get("Centipawn")
                mate = m.get("Mate")

                if cp is not None:
                    white_cp = cp if turn == "w" else -cp
                    pawns = round(white_cp / 100.0, 2)
                    score_str = f"+{pawns:.2f}" if pawns > 0 else f"{pawns:.2f}"
                elif mate is not None:
                    white_mate = mate if turn == "w" else -mate
                    if white_mate == 0:
                        score_str = "Checkmate"
                    elif white_mate > 0:
                        score_str = f"+M{white_mate}"
                    else:
                        score_str = f"-M{abs(white_mate)}"
                else:
                    score_str = "0.00"

                # Calculate centipawn loss for candidate lines #2 and #3 by comparing vs line #1
                cand_score = move_score_to_cp(m)
                if idx == 0:
                    cand_loss = 0
                    cand_class = "Best"
                else:
                    cand_loss = max(0, line1_score - cand_score)
                    cand_class = classify_candidate_loss(cand_loss)

                # Deduce SAN for candidate move
                san_move = uci_move
                try:
                    san_move = board.san(chess.Move.from_uci(uci_move))
                except Exception:
                    pass

                formatted_moves.append({
                    "rank": idx + 1,
                    "move": uci_move,
                    "san": san_move,
                    "centipawn": cp,
                    "mate": mate,
                    "score_text": score_str,
                    "cp_loss": cand_loss,
                    "classification": cand_class,
                })

            # 4. Check sacrifice for Brilliant move
            is_piece_sac = check_is_sacrifice(prev_fen, fen, last_move) if (prev_fen and last_move) else False

            # 5. Classify the played move
            if last_move:
                classification = classify_move(
                    uci_move=last_move.get("uci", ""),
                    san_move=last_move.get("san", ""),
                    fullmove_number=fullmove,
                    cp_loss=cp_loss,
                    is_top_move=is_top_move,
                    only_move_gap=only_move_gap,
                    is_piece_sac=is_piece_sac,
                    eval_before=eval_before,
                    eval_after=eval_after,
                    is_checkmate=is_checkmate,
                )
            else:
                classification = {
                    "name": "Book",
                    "symbol": "Book",
                    "glyph": "book",
                    "badge_class": "badge-book",
                    "color": "#94a3b8",
                    "bg": "rgba(148, 163, 184, 0.16)",
                    "border": "rgba(148, 163, 184, 0.4)",
                    "description": "Standard theoretical setup",
                    "cp_loss": 0,
                }

            # Determine absolute best engine move in SAN format
            best_move_san = None
            best_move_uci = None
            if prev_fen and stockfish.is_fen_valid(prev_fen) and prev_top:
                best_prev_uci = prev_top[0].get("Move")
                best_move_uci = best_prev_uci
                try:
                    prev_b = chess.Board(prev_fen)
                    best_move_san = prev_b.san(chess.Move.from_uci(best_prev_uci))
                except Exception:
                    best_move_san = best_prev_uci
            elif best_move:
                best_move_uci = best_move
                try:
                    best_move_san = board.san(chess.Move.from_uci(best_move))
                except Exception:
                    best_move_san = best_move

            # Fast evaluation response: stripped of heavy text generation
            return jsonify({
                "success": True,
                "engine": "Stockfish 19",
                "fen": fen,
                "turn": turn,
                "evaluation": formatted_eval,
                "classification": classification,
                "best_move": best_move_san,       # Absolute best engine move in SAN format (e.g. 'Rf1')
                "best_move_uci": best_move_uci,   # UCI format (e.g. 'f1d1')
                "best_move_from": best_move_uci[:2] if best_move_uci and len(best_move_uci) >= 4 else None,
                "best_move_to": best_move_uci[2:4] if best_move_uci and len(best_move_uci) >= 4 else None,
                "depth": engine_depth,
                "skill_level": active_skill,
                "target_elo": parsed_target_elo,
                "think_time": think_time,
                "search_time_ms": searchtime_ms,
                "cp_loss": cp_loss,
                "eval_before": eval_before,
                "eval_after": eval_after,
                "is_repetition": is_repetition,
                "can_claim_threefold_repetition": can_claim_draw,
                "is_draw": is_draw,
                "top_moves": formatted_moves,
            })

        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            # State Safety: Strictly restore engine to Max settings (Skill 20, Depth 15)
            try:
                stockfish.update_engine_parameters({"UCI_LimitStrength": False, "Skill Level": 20})
                stockfish.set_skill_level(20)
                stockfish.set_depth(15)
            except Exception as e:
                print(f"[!] Warning: failed to restore engine to Max settings: {e}")


def generate_comparative_explanation(
    curr_fen: str,
    prev_fen: str | None,
    last_move: dict | None,
    classification: dict | None,
    best_move_san: str | None,
    best_move_uci: str | None,
    moves: list | None = None,
    initial_fen: str | None = None,
    eval_before: int | float | None = None,
    eval_after: int | float | None = None,
    is_losing: bool | None = None,
    forces_draw: bool | None = None,
) -> dict:
    """
    Generates human-like comparative explanation comparing the played move
    against the engine's best move (e.g. 'Qd3 is an inaccuracy because it exposes the
    queen to a tempo attack. Best was Rf1. This creates a battery...').
    Also detects threefold repetition resources in losing positions.
    """
    if not last_move or not prev_fen:
        return {
            "title": "Starting Position Analysis",
            "move": "Initial Setup",
            "classification": "Book",
            "best_move": best_move_san or "e4",
            "explanation": "Standard starting position. White holds first-move initiative. Fight for central control with 1. e4 or 1. d4, develop minor pieces, and castle early for king safety.",
            "tip": "Prioritize rapid piece development and controlling the center squares."
        }

    uci = last_move.get("uci", "")
    san = last_move.get("san", uci)
    piece = last_move.get("piece", "").lower()
    color_char = last_move.get("color", "w")
    opp_side = "Black" if color_char == "w" else "White"
    from_sq = last_move.get("from", "")
    to_sq = last_move.get("to", "")
    captured = last_move.get("captured")

    class_name = classification.get("name", "Good") if classification else "Good"
    cp_loss = classification.get("cp_loss", 0) if classification else 0

    piece_names = {
        "p": "pawn", "n": "knight", "b": "bishop", "r": "rook", "q": "queen", "k": "king"
    }
    piece_name = piece_names.get(piece, "piece")

    # If best_move_san is missing, deduce from UCI
    if not best_move_san and best_move_uci:
        try:
            prev_b = chess.Board(prev_fen)
            best_move_san = prev_b.san(chess.Move.from_uci(best_move_uci))
        except Exception:
            best_move_san = best_move_uci

    best_san = best_move_san or "an optimal alternative"

    # Reconstruct python-chess Board objects with full move history for accurate draw detection
    prev_moves = moves[:-1] if (moves and len(moves) > 0) else None
    prev_board = build_board_with_history(prev_fen, moves=prev_moves, initial_fen=initial_fen)
    curr_board = build_board_with_history(curr_fen, moves=moves, initial_fen=initial_fen)
    is_checkmate = curr_board.is_checkmate()

    # Heuristic: Check if the engine's best move forces a draw/repetition in an otherwise losing position
    forces_draw_repetition = bool(forces_draw)

    # 1. Test pushing best_move_uci on prev_board to see if it forces threefold repetition
    if not forces_draw_repetition and best_move_uci and prev_board:
        try:
            test_board = prev_board.copy()
            test_mv = chess.Move.from_uci(best_move_uci)
            if test_mv in test_board.legal_moves:
                test_board.push(test_mv)
                if test_board.is_repetition(3) or test_board.can_claim_threefold_repetition():
                    forces_draw_repetition = True
        except Exception:
            pass

    # 2. Check if current board or move already forced threefold repetition
    if not forces_draw_repetition and curr_board:
        if curr_board.is_repetition(3) or curr_board.can_claim_threefold_repetition():
            forces_draw_repetition = True

    if not forces_draw_repetition and last_move and (last_move.get("is_repetition") or last_move.get("forces_draw")):
        forces_draw_repetition = True

    # 3. Determine if the moving player is in an otherwise losing position
    moving_color = chess.WHITE if color_char == "w" else chess.BLACK
    is_position_losing = False
    if is_losing is not None:
        is_position_losing = bool(is_losing)
    elif eval_before is not None and float(eval_before) <= -150:
        is_position_losing = True
    elif eval_after is not None and float(eval_after) <= -200:
        is_position_losing = True
    else:
        # Check material deficit on prev_board or curr_board
        ref_board = prev_board or curr_board
        if ref_board:
            piece_vals = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
            my_mat = sum(len(ref_board.pieces(pt, moving_color)) * val for pt, val in piece_vals.items())
            opp_mat = sum(len(ref_board.pieces(pt, not moving_color)) * val for pt, val in piece_vals.items())
            if (my_mat - opp_mat) <= -3:  # Down at least a minor piece or 3 pawns
                is_position_losing = True

    # Forced draw / repetition heuristic in an otherwise losing position
    if forces_draw_repetition and is_position_losing:
        if class_name in ["Inaccuracy", "Mistake", "Blunder", "Miss"]:
            article = "an" if class_name.lower()[0] in "aeiou" else "a"
            explanation = f"{san} is {article} {class_name.lower()}. You must play this to force a draw by repetition. Otherwise, your opponent has a crushing advantage. Best was {best_san}."
            title = f"Forced Draw Missed: {san} vs {best_san}"
            tip = "When facing a crushing disadvantage, always calculate perpetual checks and repetition resources to salvage a draw."
        else:
            explanation = f"{san} forces a draw by repetition! You must play this to force a draw by repetition. Otherwise, your opponent has a crushing advantage."
            title = f"Forced Draw by Repetition: {san}"
            tip = "Outstanding defensive awareness! Forcing a draw by repetition salvages a half-point from an otherwise lost position."

        return {
            "title": title,
            "move": san,
            "classification": class_name,
            "best_move": best_san,
            "explanation": explanation,
            "tip": tip,
            "is_repetition": True,
            "forces_draw": True,
        }

    # Determine why played move was suboptimal or flawed
    flaw_reason = ""
    if piece == "q" and class_name in ["Inaccuracy", "Mistake", "Blunder"]:
        # Exposes the queen to a tempo attack (user's exact example)
        flaw_reason = "it exposes the queen to a tempo attack"
    elif class_name == "Blunder":
        if is_checkmate:
            flaw_reason = f"it allows {opp_side.lower()} to deliver an immediate checkmate"
        else:
            try:
                to_idx = chess.parse_square(to_sq)
                opp_color = (color_char == "b")
                has_attackers = bool(curr_board.attackers(opp_color, to_idx))
                has_defenders = bool(curr_board.attackers(not opp_color, to_idx))
                if has_attackers and not has_defenders:
                    flaw_reason = f"it leaves the {piece_name} hanging on {to_sq} without defense"
                elif cp_loss >= 300:
                    flaw_reason = "it suffers severe material loss from an immediate tactical sequence"
                else:
                    flaw_reason = f"it allows a decisive tactical breakthrough for {opp_side.lower()}"
            except Exception:
                flaw_reason = "it suffers a massive tactical setback"
    elif class_name == "Mistake":
        if captured:
            flaw_reason = f"it initiates an unfavorable material exchange on {to_sq}"
        elif piece in ["b", "n"]:
            flaw_reason = f"it misplaces the {piece_name} and concedes central initiative"
        else:
            flaw_reason = f"it compromises piece coordination and surrenders vital spatial control"
    elif class_name == "Inaccuracy":
        if piece == "q":
            flaw_reason = "it exposes the queen to a tempo attack"
        elif piece in ["b", "n"]:
            flaw_reason = f"it misplaces the {piece_name} on {to_sq} where it lacks active scope"
        elif piece == "p":
            flaw_reason = f"it weakens key central squares and restricts piece mobility"
        else:
            flaw_reason = "it allows the opponent easy counterplay and equalizes the position"
    elif class_name == "Miss":
        flaw_reason = "it fails to find a decisive tactical strike that would have sealed the advantage"

    # Determine purpose of the engine's best move
    best_purpose = ""
    best_piece_char = "p"
    best_to_sq = ""
    if best_move_uci and len(best_move_uci) >= 4:
        try:
            best_from_idx = chess.parse_square(best_move_uci[:2])
            p = prev_board.piece_at(best_from_idx)
            best_piece_char = p.piece_type if p else chess.PAWN
            best_to_sq = best_move_uci[2:4]
        except Exception:
            pass

    if "O-O" in best_san:
        best_purpose = "This secures king safety and activates the rook onto the central file."
    elif best_san.startswith("R") or best_piece_char == chess.ROOK:
        file_letter = best_to_sq[0] if best_to_sq else "f"
        best_purpose = f"This creates a battery along the {file_letter}-file, reinforcing piece harmony and applying decisive pressure."
    elif best_san.startswith("Q") or best_piece_char == chess.QUEEN:
        best_purpose = "This activates the queen to dominate central lines and coordinate an immediate kingside threat."
    elif best_san.startswith("B") or best_piece_char == chess.BISHOP:
        best_purpose = "This pins the enemy piece along the diagonal and prevents counterplay."
    elif best_san.startswith("N") or best_piece_char == chess.KNIGHT:
        best_purpose = "This centralizes the knight on a dominant outpost, radiating control over critical squares."
    elif best_piece_char == chess.PAWN:
        best_purpose = "This anchors the central pawn chain and denies enemy minor pieces active infiltration squares."
    else:
        best_purpose = "This preserves optimal positional harmony and maintains the advantage."

    # Build comparative explanation
    if class_name in ["Inaccuracy", "Mistake", "Blunder", "Miss"]:
        article = "an" if class_name.lower()[0] in "aeiou" else "a"
        explanation = f"{san} is {article} {class_name.lower()} because {flaw_reason}. Best was {best_san}. {best_purpose}"
        title = f"{class_name}: {san} vs Best {best_san}"
        tip = f"Before playing {san}, look for opponent forcing replies and evaluate {best_san}."
    elif class_name in ["Brilliant", "Great"]:
        explanation = f"{san} is {class_name.lower()}! It seizes tactical initiative with precision. Best was {best_san}. {best_purpose}"
        title = f"{class_name} Move: {san}"
        tip = "Masterful calculation! Keep up the pressure and convert your tactical edge."
    elif class_name == "Book":
        explanation = f"{san} is standard theoretical opening play ({class_name}). Best continuation is {best_san}. {best_purpose}"
        title = f"Book Opening: {san}"
        tip = "Standard book continuation. Transition smoothly into your planned middlegame strategy."
    else:  # Good / Best
        explanation = f"{san} is a solid top engine choice ({class_name}). Best was {best_san}. {best_purpose}"
        title = f"Solid Move: {san}"
        tip = "Solid piece coordination! Continue executing your positional plan."

    return {
        "title": title,
        "move": san,
        "classification": class_name,
        "best_move": best_san,
        "explanation": explanation,
        "tip": tip
    }


@app.route("/explain_move", methods=["GET", "POST"])
def explain_move():
    """
    On-Demand Comparative Explanation Endpoint:
    Generates human-like comparative text explanation on demand when requested.
    """
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
    else:
        data = request.args.to_dict()

    curr_fen = data.get("fen", "").strip()
    prev_fen = data.get("prev_fen", "").strip() or None
    last_move = data.get("last_move")
    classification = data.get("classification")
    best_move_san = data.get("best_move")
    best_move_uci = data.get("best_move_uci")
    moves = data.get("moves") or data.get("history") or data.get("move_stack")
    initial_fen = data.get("initial_fen")
    eval_before = data.get("eval_before")
    eval_after = data.get("eval_after")
    is_losing = data.get("is_losing")
    forces_draw = data.get("forces_draw")

    explanation_data = generate_comparative_explanation(
        curr_fen=curr_fen,
        prev_fen=prev_fen,
        last_move=last_move,
        classification=classification,
        best_move_san=best_move_san,
        best_move_uci=best_move_uci,
        moves=moves,
        initial_fen=initial_fen,
        eval_before=eval_before,
        eval_after=eval_after,
        is_losing=is_losing,
        forces_draw=forces_draw,
    )
    return jsonify({
        "success": True,
        **explanation_data
    })


def win_probability(centipawns: float) -> float:
    """
    Standard Chess.com / Lichess sigmoid win probability model:
    Maps centipawn advantage to win percentage (0 to 100).
    """
    clamped_cp = max(-10000.0, min(10000.0, float(centipawns)))
    return 100.0 / (1.0 + 10.0 ** (-clamped_cp / 400.0))


def compute_move_accuracy(w_before: float, w_after: float) -> float:
    """
    Caps2 formula for move accuracy based on win probability delta:
    acc = 103.1668 * exp(-0.04354 * delta_w) - 3.1669
    Clamped strictly between 0.0 and 100.0.
    """
    delta_w = max(0.0, w_before - w_after)
    acc = 103.1668 * math.exp(-0.04354 * delta_w) - 3.1669
    return max(0.0, min(100.0, acc))


@app.route("/calculate_accuracy", methods=["POST"])
def calculate_accuracy():
    """
    Calculates End Game Accuracy scores for both White and Black
    using the Win Probability (sigmoid) math model averaged across all moves.
    Returns strictly simple floats formatted to 1 decimal place without '%' symbol.
    """
    data = request.get_json(silent=True) or {}
    history = data.get("history", [])

    if not history:
        return jsonify({
            "success": True,
            "white": 100.0,
            "black": 100.0
        })

    white_accs = []
    black_accs = []

    for item in history:
        color = item.get("color") or (item.get("moveData") or {}).get("color", "w")
        eval_before = item.get("eval_before")
        eval_after = item.get("eval_after")
        cp_loss = item.get("cp_loss")

        if eval_before is not None and eval_after is not None:
            try:
                eb = float(eval_before)
                ea = float(eval_after)
                if color == "w":
                    wb = win_probability(eb)
                    wa = win_probability(ea)
                else:
                    wb = win_probability(-eb)
                    wa = win_probability(-ea)
                acc = compute_move_accuracy(wb, wa)
            except Exception:
                acc = 100.0
        elif cp_loss is not None:
            try:
                loss = float(cp_loss)
                wb = win_probability(0)
                wa = win_probability(-loss)
                acc = compute_move_accuracy(wb, wa)
            except Exception:
                acc = 100.0
        else:
            acc = 100.0

        if color == "w":
            white_accs.append(acc)
        else:
            black_accs.append(acc)

    white_final = round(sum(white_accs) / len(white_accs), 1) if white_accs else 100.0
    black_final = round(sum(black_accs) / len(black_accs), 1) if black_accs else 100.0

    return jsonify({
        "success": True,
        "white": white_final,
        "black": black_final
    })


def configure_bot_elo(stockfish_instance: Stockfish, target_elo: int) -> tuple[int, int]:
    """
    Configures Stockfish parameters to simulate human play and flaws at a given target Elo.
    Uses UCI_LimitStrength and UCI_Elo where supported (Elo >= 1320 in Stockfish).
    For lower ratings or fallback, manually maps Elo to Depth and Skill Level.
    Returns (depth, skill_level).
    """
    try:
        target_elo = int(target_elo)
    except Exception:
        target_elo = 1500

    target_elo = max(400, min(3200, target_elo))

    # Dynamically map Elo to Depth and Skill Level
    depth, skill = map_elo_to_depth_and_skill(target_elo)

    # Stockfish official UCI_Elo range is [1320, 3190]
    if target_elo >= 1320:
        try:
            stockfish_instance.set_elo_rating(min(3190, target_elo))
            stockfish_instance.set_depth(depth)
            return depth, skill
        except Exception as e:
            print(f"[!] Warning: set_elo_rating failed, falling back to manual mapping: {e}")

    # For Elo < 1320 or fallback when UCI_LimitStrength cannot reach target Elo
    try:
        stockfish_instance.update_engine_parameters({"UCI_LimitStrength": False})
        stockfish_instance.set_skill_level(skill)
        stockfish_instance.set_depth(depth)
    except Exception as e:
        print(f"[!] Warning: manual engine parameter update error: {e}")

    return depth, skill


@app.route("/play_bot", methods=["POST", "GET"])
def play_bot():
    """
    Human-Like Sparring Mode endpoint:
    Accepts JSON containing fen, move_history, and target_elo.
    Maps target_elo to Stockfish parameters (UCI_LimitStrength/UCI_Elo or Depth/Skill Level).
    Simulates dynamic human thinking delay via time.sleep():
    - Opening (move_history < 10): random 0.5s to 1.5s
    - Complex/Tactical (last move contains 'x' or '+'): random 4.0s to 8.0s
    - Standard positions: random 2.0s to 4.5s
    Returns the chosen engine move in SAN format.
    """
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        fen = data.get("fen", "").strip() or chess.STARTING_FEN
        move_history = data.get("move_history") or data.get("history") or []
        target_elo = data.get("target_elo", 1500)
    else:
        fen = request.args.get("fen", "").strip() or chess.STARTING_FEN
        move_history = request.args.get("move_history") or []
        target_elo = request.args.get("target_elo", 1500)

    try:
        target_elo = int(target_elo)
    except Exception:
        target_elo = 1500

    target_elo = max(400, min(3200, target_elo))

    with engine_lock:
        if not stockfish.is_fen_valid(fen):
            return jsonify({"success": False, "error": f"Invalid FEN position string: '{fen}'"}), 400

        board = chess.Board(fen)
        if board.is_game_over():
            return jsonify({
                "success": True,
                "game_over": True,
                "move": None,
                "san": None,
                "uci": None,
                "is_checkmate": board.is_checkmate(),
                "is_stalemate": board.is_stalemate(),
                "thinking_time": 0.0,
            })

        depth = 7
        skill = 7
        best_move_uci = None

        try:
            # 1. Map target_elo to Stockfish parameters to simulate human flaws
            depth, skill = configure_bot_elo(stockfish, target_elo)

            # 2. Set board position and query best move
            stockfish.set_fen_position(fen)
            best_move_uci = stockfish.get_best_move()
        finally:
            # 3. Always restore engine to default analysis parameters
            try:
                stockfish.update_engine_parameters({"UCI_LimitStrength": False, "Skill Level": 20})
                stockfish.set_depth(15)
            except Exception as e:
                print(f"[!] Warning: failed to restore engine parameters: {e}")

    if not best_move_uci:
        return jsonify({
            "success": True,
            "game_over": True,
            "move": None,
            "san": None,
            "uci": None,
            "is_checkmate": board.is_checkmate(),
            "is_stalemate": board.is_stalemate(),
            "thinking_time": 0.0,
        })

    # Convert engine move to SAN format
    try:
        move_obj = chess.Move.from_uci(best_move_uci)
        san_move = board.san(move_obj)
    except Exception:
        san_move = best_move_uci

    # 4. Dynamic Time Delay: Simulate human thinking
    history_count = 0
    last_move_text = ""

    if isinstance(move_history, list):
        history_count = len(move_history)
        if move_history:
            last_item = move_history[-1]
            if isinstance(last_item, dict):
                last_move_text = last_item.get("san") or last_item.get("uci") or ""
            else:
                last_move_text = str(last_item)
    elif isinstance(move_history, int):
        history_count = move_history
    elif isinstance(move_history, str) and move_history.strip():
        tokens = move_history.strip().split()
        history_count = len(tokens)
        last_move_text = tokens[-1]
    else:
        history_count = (board.fullmove_number - 1) * 2 + (1 if board.turn == chess.BLACK else 0)

    # Complex/Tactical detection (last move contains 'x' or '+') or position is in check
    is_tactical = (
        "x" in last_move_text or
        "+" in last_move_text or
        "#" in last_move_text or
        board.is_check()
    )

    if is_tactical:
        delay = random.uniform(4.0, 8.0)
    elif history_count < 10:
        delay = random.uniform(0.5, 1.5)
    else:
        delay = random.uniform(2.0, 4.5)

    # Simulate human thinking delay before responding
    time.sleep(delay)

    return jsonify({
        "success": True,
        "move": san_move,
        "san": san_move,
        "uci": best_move_uci,
        "from": best_move_uci[:2],
        "to": best_move_uci[2:4],
        "promotion": best_move_uci[4] if len(best_move_uci) == 5 else None,
        "target_elo": target_elo,
        "thinking_time": round(delay, 2),
        "depth": depth,
        "skill_level": skill,
        "is_tactical": is_tactical,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[*] Starting Chess Analysis Dashboard with Stockfish 19 on http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=True)
