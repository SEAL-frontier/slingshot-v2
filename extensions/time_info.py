from datetime import datetime, timezone
from cornnor.modules import tool


SYSTEM_PROMPT = "The bot has access to real-time system clock and timezone tools."


@tool(name="get_current_time", description="Get the current date and time in UTC or local server time")
def get_current_time(include_utc: bool = True) -> str:
    """Returns the current date and time formatted nicely."""
    now_local = datetime.now()
    now_utc = datetime.now(timezone.utc)
    if include_utc:
        return f"Local Time: {now_local.strftime('%Y-%m-%d %H:%M:%S')}\nUTC Time: {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    return f"Local Time: {now_local.strftime('%Y-%m-%d %H:%M:%S')}"


def on_load(bot):
    print("[time_info] Extension loaded.")


def on_unload(bot):
    print("[time_info] Extension unloaded.")
