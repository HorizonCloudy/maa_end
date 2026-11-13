"""生成/重建 data/gamescaling/endfield_system_prompt.txt。

系统提示词是训练格式敏感的：分节头（**Game Rules** 等）与 Output Format /
Key Naming Rules 段必须与 gamescaling/prompt.py 的模板逐字一致。所以不要手改
那些段——改本脚本里的 RULES / KEYS（终末地实际玩法与键位），再跑：

    cd <插件根目录>
    python data/gamescaling/build_prompt.py

生成结果与 gamescaling.prompt.build_system_prompt 的输出逐字节一致
（tests/test_gamescaling_logic.py 有校验）。键位是草案：进游戏设置里核对，
不一致就改 KEYS 再生成。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]

GAME_NAME = "Arknights Endfield"

RULES = """\
You are playing Arknights Endfield, a third-person action RPG. You control one active
operator out of a squad; the goal is to complete the current objective — usually
clearing enemies, exploring the field, or reaching the destination — while keeping
your squad alive.

## What you see on screen
- Third-person view of your active operator, camera behind them.
- The HUD shows squad HP, skill icons with cooldowns, and the objective tracker.
- Enemies have HP bars above them; heavy attacks are telegraphed with red or white
  flash markers — move or dodge out of those.
- Pickups and interactable objects glow; stand close and press F.

## Rules
- Taking damage reduces the active operator's HP; an operator that falls must be
  switched out, and losing the whole squad fails the run, so dodging matters as
  much as attacking.
- Dodging (Shift while moving) has a perfect-dodge window: dodging just before an
  enemy hit lands triggers a brief counter window — use it.
- Normal attacks (LB) build up resources; skills (E, Q) and the ultimate (R) spend
  or charge them. Spend them when enemies cluster.
- Switching operators (one/two/three) starts a short cooldown; use it to extend
  combos or rescue a low-HP operator.
- On a menu screen, navigate with the mouse and confirm with LB, or press Esc to
  back out."""

KEYS = [
    ("W", "Move forward"),
    ("A", "Move left"),
    ("S", "Move backward"),
    ("D", "Move right"),
    ("Space", "Jump"),
    ("Shift", "Sprint / dodge while moving"),
    ("LB", "Normal attack"),
    ("RB", "Aim / heavy attack"),
    ("E", "Skill"),
    ("Q", "Secondary skill"),
    ("R", "Ultimate"),
    ("F", "Interact / pick up"),
    ("Tab", "Map"),
    ("Esc", "Menu / back"),
    ("one", "Switch to operator 1"),
    ("two", "Switch to operator 2"),
    ("three", "Switch to operator 3"),
]


def main() -> None:
    sys.path.insert(0, str(_PLUGIN_ROOT / "maaend"))
    from gamescaling.prompt import build_system_prompt

    prompt = build_system_prompt(GAME_NAME, rules=RULES, key_bindings=KEYS,
                                 mouse_aim=True)
    out = Path(__file__).with_name("endfield_system_prompt.txt")
    out.write_text(prompt + "\n", encoding="utf-8")
    print(f"written: {out} ({len(prompt)} chars)")


if __name__ == "__main__":
    main()
