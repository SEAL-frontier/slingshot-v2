import random
import re
from cornnor.modules import tool


@tool(name="roll_dice", description="Roll dice using standard RPG dice notation (e.g., '1d20', '3d6+5', '2d10-1')")
def roll_dice(dice_notation: str = "1d6") -> str:
    """
    Rolls dice following the NdS+M or NdS-M format.
    Example: 2d20+3 -> Rolls two 20-sided dice and adds 3.
    """
    pattern = r"^\s*(\d+)?d(\d+)(?:([+-])(\d+))?\s*$"
    match = re.match(pattern, dice_notation.strip().lower())
    if not match:
        return f"Invalid dice notation '{dice_notation}'. Use format like '1d6', '2d20+4', or 'd100'."

    count = int(match.group(1)) if match.group(1) else 1
    sides = int(match.group(2))
    sign = match.group(3)
    modifier = int(match.group(4)) if match.group(4) else 0

    if count < 1 or count > 100:
        return "Dice count must be between 1 and 100."
    if sides < 2 or sides > 1000:
        return "Dice sides must be between 2 and 1000."

    rolls = [random.randint(1, sides) for _ in range(count)]
    subtotal = sum(rolls)

    if sign == "+":
        total = subtotal + modifier
        mod_str = f" + {modifier}"
    elif sign == "-":
        total = subtotal - modifier
        mod_str = f" - {modifier}"
    else:
        total = subtotal
        mod_str = ""

    if count == 1 and not mod_str:
        return f"🎲 Rolled {dice_notation}: **{total}**"
    return f"🎲 Rolled {dice_notation}: ({' + '.join(map(str, rolls))}){mod_str} = **{total}**"
