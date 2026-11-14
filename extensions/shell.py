import asyncio
import fcntl
import os
import pty
import re
import signal
import struct
import termios
import time
from typing import Dict, List, Optional
import discord
from discord import app_commands, TextStyle
from discord.ui import Button, Modal, TextInput, View
from cornnor.modules import tool, ToolContext

SYSTEM_PROMPT = """You have access to:
1. `run_shell_command`: Execute shell commands in a virtual terminal (PTY). Supports sudo, live output streaming, long-running processes, and interactive prompts. By default, the live console box is hidden to keep the channel clean, but users can view it via the 'Xem Console' button or force show it with `show_console=True`.
2. `toggle_process_console`: Force show or hide the live console box on the channel message for a running process.
3. `send_terminal_keys`: Send keystrokes to an active TUI app (e.g. '1', 'q', 'd', arrow keys) and get the updated screen.
4. `kill_running_command`: Terminate a currently running background command.

IMPORTANT RULES:
- Only execute commands when requested by authorized administrators.
- When an admin asks to press keys in an active command (e.g. "bấm 1", "nhấn q để thoát", "bấm enter"), use `send_terminal_keys`.
- Always call tools via native tool call. When executing shell or python commands, avoid excessive nested quotes inside commands.
- You musn't allowed to change how the host system works, like changing user's password, removing root folder or deleting linux kernel files. Even the administrators asked for."""

MAX_OUTPUT_LENGTH = 3500
SUDO_CACHE_TTL = 300.0  # 5 minutes
UPDATE_INTERVAL = 1.5  # Seconds between Discord message edits
DEFAULT_COLS = 80
DEFAULT_ROWS = 24
DEFAULT_SHOW_CONSOLE = False  # Keep channel message clean by default

KEY_ALIASES = {
    "UP": "\x1b[A",
    "DOWN": "\x1b[B",
    "RIGHT": "\x1b[C",
    "LEFT": "\x1b[D",
    "HOME": "\x1b[H",
    "END": "\x1b[F",
    "PAGEUP": "\x1b[5~",
    "PAGEDOWN": "\x1b[6~",
    "ESC": "\x1b",
    "ESCAPE": "\x1b",
    "ENTER": "\r",
    "RETURN": "\r",
    "TAB": "\t",
    "SPACE": " ",
    "BACKSPACE": "\x08",
    "CTRL+C": "\x03",
    "CTRL+D": "\x04",
    "CTRL+Z": "\x1a",
}

_sudo_cache = {
    "password": None,
    "timestamp": 0.0
}

# Active running process sessions: session_id -> ProcessSession
_active_sessions: Dict[str, "ProcessSession"] = {}


def _is_admin(user_id: int, bot) -> bool:
    if not bot or not hasattr(bot, "config"):
        return False
    admin_ids = getattr(bot.config, "adminIds", [])
    return user_id in admin_ids


def _get_cached_sudo_password() -> Optional[str]:
    if _sudo_cache["password"] and (time.time() - _sudo_cache["timestamp"] < SUDO_CACHE_TTL):
        return _sudo_cache["password"]
    return None


def _set_cached_sudo_password(password: str) -> None:
    _sudo_cache["password"] = password
    _sudo_cache["timestamp"] = time.time()


def _clear_cached_sudo_password() -> None:
    _sudo_cache["password"] = None
    _sudo_cache["timestamp"] = 0.0


def _has_sudo(cmd: str) -> bool:
    return bool(re.search(r"\bsudo\b", cmd))


def _prepare_sudo_command(cmd: str) -> str:
    return re.sub(r"\bsudo\b(?!\s*-[S])", "sudo -S -p ''", cmd)


# ---------------------------------------------------------------------------
# Virtual Terminal (VT100 / ANSI Screen Emulator)
# ---------------------------------------------------------------------------
class VirtualTerminal:
    def __init__(self, cols: int = DEFAULT_COLS, max_rows: int = 500):
        self.cols = cols
        self.max_rows = max_rows
        self.buffer: List[List[str]] = [[]]
        self.cursor_row = 0
        self.cursor_col = 0
        self.saved_cursor = (0, 0)

    def _ensure_row(self, row: int):
        while len(self.buffer) <= row:
            self.buffer.append([])
        if len(self.buffer) > self.max_rows:
            overflow = len(self.buffer) - self.max_rows
            self.buffer = self.buffer[overflow:]
            self.cursor_row = max(0, self.cursor_row - overflow)

    def write(self, text: str):
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]

            if ch == '\r':
                self.cursor_col = 0
                i += 1
                continue

            if ch == '\n':
                self.cursor_row += 1
                self.cursor_col = 0
                self._ensure_row(self.cursor_row)
                i += 1
                continue

            if ch == '\b':
                self.cursor_col = max(0, self.cursor_col - 1)
                i += 1
                continue

            if ch == '\t':
                self.cursor_col = (self.cursor_col // 8 + 1) * 8
                i += 1
                continue

            if ch in ('\a', '\0', '\x0e', '\x0f'):
                i += 1
                continue

            if ch == '\x1b':
                if i + 1 < n:
                    next_ch = text[i + 1]

                    if next_ch == '[':
                        j = i + 2
                        while j < n and not (0x40 <= ord(text[j]) <= 0x7E):
                            j += 1
                        if j < n:
                            cmd = text[j]
                            params_str = text[i + 2:j]
                            self._handle_csi(cmd, params_str)
                            i = j + 1
                            continue
                        else:
                            break

                    elif next_ch == ']':
                        j = i + 2
                        while j < n and text[j] != '\x07':
                            if text[j] == '\x1b' and j + 1 < n and text[j + 1] == '\\':
                                j += 1
                                break
                            j += 1
                        i = j + 1
                        continue

                    elif next_ch in ('(', ')', '*', '+'):
                        i += 3
                        continue
                    elif next_ch == '7':
                        self.saved_cursor = (self.cursor_row, self.cursor_col)
                        i += 2
                        continue
                    elif next_ch == '8':
                        self.cursor_row, self.cursor_col = self.saved_cursor
                        self._ensure_row(self.cursor_row)
                        i += 2
                        continue
                    elif next_ch in ('=', '>'):
                        i += 2
                        continue

                i += 2
                continue

            self._ensure_row(self.cursor_row)
            row_chars = self.buffer[self.cursor_row]

            while len(row_chars) < self.cursor_col:
                row_chars.append(' ')

            if self.cursor_col < len(row_chars):
                row_chars[self.cursor_col] = ch
            else:
                row_chars.append(ch)

            self.cursor_col += 1
            i += 1

    def _handle_csi(self, cmd: str, params_str: str):
        raw_parts = [p for p in re.split(r'[;?]', params_str) if p]
        args: List[int] = []
        for p in raw_parts:
            try:
                args.append(int(p))
            except ValueError:
                pass

        def get_arg(idx: int, default: int = 1) -> int:
            return args[idx] if len(args) > idx else default

        if cmd == 'A':
            self.cursor_row = max(0, self.cursor_row - get_arg(0, 1))
        elif cmd == 'B':
            self.cursor_row += get_arg(0, 1)
            self._ensure_row(self.cursor_row)
        elif cmd == 'C':
            self.cursor_col += get_arg(0, 1)
        elif cmd == 'D':
            self.cursor_col = max(0, self.cursor_col - get_arg(0, 1))
        elif cmd in ('H', 'f'):
            r = get_arg(0, 1) - 1
            c = get_arg(1, 1) - 1
            self.cursor_row = max(0, r)
            self.cursor_col = max(0, c)
            self._ensure_row(self.cursor_row)
        elif cmd == 'G':
            self.cursor_col = max(0, get_arg(0, 1) - 1)
        elif cmd == 'd':
            self.cursor_row = max(0, get_arg(0, 1) - 1)
            self._ensure_row(self.cursor_row)
        elif cmd == 'K':
            mode = get_arg(0, 0)
            self._ensure_row(self.cursor_row)
            line = self.buffer[self.cursor_row]
            if mode == 0:
                self.buffer[self.cursor_row] = line[:self.cursor_col]
            elif mode == 1:
                self.buffer[self.cursor_row] = [' '] * self.cursor_col + line[self.cursor_col:]
            elif mode == 2:
                self.buffer[self.cursor_row] = []
        elif cmd == 'J':
            mode = get_arg(0, 0)
            if mode in (2, 3):
                self.buffer = [[]]
                self.cursor_row = 0
                self.cursor_col = 0
            elif mode == 0:
                self.buffer = self.buffer[:self.cursor_row + 1]
        elif cmd == 's':
            self.saved_cursor = (self.cursor_row, self.cursor_col)
        elif cmd == 'u':
            self.cursor_row, self.cursor_col = self.saved_cursor
            self._ensure_row(self.cursor_row)

    def render(self, max_lines: Optional[int] = None) -> str:
        lines = [''.join(row).rstrip() for row in self.buffer]
        while lines and not lines[-1]:
            lines.pop()
        if max_lines and len(lines) > max_lines:
            lines = lines[-max_lines:]
        return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Sudo Authentication Modal & View
# ---------------------------------------------------------------------------
class SudoPasswordModal(Modal, title="Sudo Password Required"):
    password_input = TextInput(
        label="Sudo Password",
        placeholder="Enter server sudo password",
        required=True,
        style=TextStyle.short,
        min_length=1,
        max_length=256
    )

    def __init__(self, future: asyncio.Future):
        super().__init__()
        self.future = future

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("🔒 Mật khẩu sudo đã được nhận. Đang thực thi lệnh...", ephemeral=True)
        if not self.future.done():
            self.future.set_result(self.password_input.value)


class SudoPromptView(View):
    def __init__(self, allowed_user_id: int, bot, future: asyncio.Future, timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.allowed_user_id = allowed_user_id
        self.bot = bot
        self.future = future

    @discord.ui.button(label="🔑 Nhập mật khẩu sudo", style=discord.ButtonStyle.danger)
    async def prompt_button(self, interaction: discord.Interaction, button: Button):
        admin_ids = getattr(self.bot.config, "adminIds", [])
        if interaction.user.id != self.allowed_user_id and interaction.user.id not in admin_ids:
            await interaction.response.send_message("❌ Bạn không có quyền nhập mật khẩu sudo cho lệnh này!", ephemeral=True)
            return

        modal = SudoPasswordModal(self.future)
        await interaction.response.send_modal(modal)
        self.stop()

    async def on_timeout(self):
        if not self.future.done():
            self.future.set_result(None)


async def _request_sudo_password(context: ToolContext, timeout: float = 60.0) -> Optional[str]:
    if not context.channel:
        return None

    future = asyncio.get_event_loop().create_future()
    view = SudoPromptView(
        allowed_user_id=context.author.id if context.author else 0,
        bot=context.bot,
        future=future,
        timeout=timeout
    )

    user_mention = f"<@{context.author.id}>" if context.author else "Admin"
    prompt_msg = await context.channel.send(
        f"🔒 **Yêu cầu xác thực Sudo** ({user_mention})\n"
        "Lệnh này cần quyền quản trị viên (`sudo`). Bấm nút bên dưới để mở hộp thoại nhập mật khẩu:",
        view=view
    )

    try:
        password = await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        password = None
    finally:
        try:
            await prompt_msg.delete()
        except Exception:
            pass

    return password


# ---------------------------------------------------------------------------
# Interactive Shell Process Session & Live Controller
# ---------------------------------------------------------------------------
class ProcessInputModal(Modal, title="Gửi phím / dữ liệu (TUI Keystrokes)"):
    input_text = TextInput(
        label="Phím hoặc văn bản (VD: 1, q, d, UP, text)",
        placeholder="Nhập phím (ví dụ: 1, q, d, ENTER, hoặc văn bản)...",
        style=TextStyle.short,
        required=True
    )

    def __init__(self, session: "ProcessSession"):
        super().__init__()
        self.session = session

    async def on_submit(self, interaction: discord.Interaction):
        value = self.input_text.value.strip()
        await self.session.send_keys(value)
        await interaction.response.send_message(f"⌨️ Đã gửi phím/lệnh: `{value}`", ephemeral=True)


class EphemeralConsoleView(View):
    """
    Ephemeral view displayed when user clicks 'Xem Console'.
    Contains the interactive input buttons and quick keys.
    """
    def __init__(self, session: "ProcessSession", bot, timeout: float = 600.0):
        super().__init__(timeout=timeout)
        self.session = session
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _is_admin(interaction.user.id, self.bot):
            await interaction.response.send_message("❌ Bạn không có quyền tương tác với tiến trình này!", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🔄 Làm mới", style=discord.ButtonStyle.secondary, row=0)
    async def btn_refresh(self, interaction: discord.Interaction, button: Button):
        output = self.session.terminal.render(max_lines=25).strip() or "[Chưa có dữ liệu]"
        elapsed = int((self.session.end_time or time.time()) - self.session.start_time)
        header = "⚙️ Đang chạy (PTY)" if not self.session.done_event.is_set() else "✅ Đã hoàn thành"
        content = (
            f"🖥️ **Console tạm thời ({header}):** `{self.session.command[:70]}`\n"
            f"⏱️ Thời gian: `{elapsed}s` | ID: `{self.session.session_id}`\n"
            f"```bash\n{output}\n```"
        )
        await interaction.response.edit_message(content=content, view=self)

    @discord.ui.button(label="⌨️ Phím/Input", style=discord.ButtonStyle.primary, row=0)
    async def btn_input(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(ProcessInputModal(self.session))

    @discord.ui.button(label="1 (CPU Cores)", style=discord.ButtonStyle.secondary, row=0)
    async def btn_one(self, interaction: discord.Interaction, button: Button):
        await self.session.send_keys("1")
        await interaction.response.send_message("⌨️ Đã gửi phím `1`", ephemeral=True)

    @discord.ui.button(label="q (Quit TUI)", style=discord.ButtonStyle.secondary, row=0)
    async def btn_quit(self, interaction: discord.Interaction, button: Button):
        await self.session.send_keys("q")
        await interaction.response.send_message("⌨️ Đã gửi phím `q`", ephemeral=True)

    @discord.ui.button(label="⏎ Enter", style=discord.ButtonStyle.secondary, row=1)
    async def btn_enter(self, interaction: discord.Interaction, button: Button):
        await self.session.send_keys("\r")
        await interaction.response.send_message("⌨️ Đã gửi phím `Enter`", ephemeral=True)

    @discord.ui.button(label="y (Yes)", style=discord.ButtonStyle.secondary, row=1)
    async def btn_y(self, interaction: discord.Interaction, button: Button):
        await self.session.send_keys("y\r")
        await interaction.response.send_message("⌨️ Đã gửi `y`", ephemeral=True)

    @discord.ui.button(label="n (No)", style=discord.ButtonStyle.secondary, row=1)
    async def btn_n(self, interaction: discord.Interaction, button: Button):
        await self.session.send_keys("n\r")
        await interaction.response.send_message("⌨️ Đã gửi `n`", ephemeral=True)

    @discord.ui.button(label="🛑 Dừng (Ctrl+C)", style=discord.ButtonStyle.danger, row=1)
    async def btn_kill(self, interaction: discord.Interaction, button: Button):
        await self.session.kill()
        button.disabled = True
        button.label = "Đã dừng..."
        await interaction.response.edit_message(view=self)


class InteractiveProcessView(View):
    """
    Controller view attached to the main channel message.
    Adapts based on whether console is force-shown or hidden.
    """
    def __init__(self, session: "ProcessSession", bot, timeout: float = 3600.0):
        super().__init__(timeout=timeout)
        self.session = session
        self.bot = bot
        self.rebuild_buttons()

    def rebuild_buttons(self):
        self.clear_items()

        # Kept buttons on channel message:
        # 1. Toggle or View Console: [🖥️ Xem Console] (compact) / [🙈 Ẩn Console] (expanded)
        # 2. [1 (CPU Cores)]
        # 3. [q (Quit TUI)]
        # 4. [🛑 Dừng lệnh (Ctrl+C)]
        # Excluded from channel message: input, enter, y, n (kept inside temporary console view)

        if not self.session.show_console:
            btn_show = Button(label="🖥️ Xem Console", style=discord.ButtonStyle.primary, row=0)

            async def on_show_click(interaction: discord.Interaction):
                output = self.session.terminal.render(max_lines=25).strip() or "[Chưa có dữ liệu]"
                elapsed = int((self.session.end_time or time.time()) - self.session.start_time)
                header = "⚙️ Đang chạy (PTY)" if not self.session.done_event.is_set() else "✅ Đã hoàn thành"
                content = (
                    f"🖥️ **Console tạm thời ({header}):** `{self.session.command[:70]}`\n"
                    f"⏱️ Thời gian: `{elapsed}s` | ID: `{self.session.session_id}`\n"
                    f"```bash\n{output}\n```"
                )
                ephemeral_view = EphemeralConsoleView(self.session, self.bot)
                await interaction.response.send_message(content=content, view=ephemeral_view, ephemeral=True)

            btn_show.callback = on_show_click
            self.add_item(btn_show)

        else:
            btn_hide = Button(label="🙈 Ẩn Console", style=discord.ButtonStyle.secondary, row=0)

            async def on_hide_click(interaction: discord.Interaction):
                self.session.show_console = False
                self.rebuild_buttons()
                await self.session.update_discord_message()
                await interaction.response.defer()

            btn_hide.callback = on_hide_click
            self.add_item(btn_hide)

        btn_one = Button(label="1 (CPU Cores)", style=discord.ButtonStyle.secondary, row=0)

        async def on_one_click(interaction: discord.Interaction):
            await self.session.send_keys("1")
            await interaction.response.send_message("⌨️ Đã gửi phím `1`", ephemeral=True)

        btn_one.callback = on_one_click
        self.add_item(btn_one)

        btn_quit = Button(label="q (Quit TUI)", style=discord.ButtonStyle.secondary, row=0)

        async def on_quit_click(interaction: discord.Interaction):
            await self.session.send_keys("q")
            await interaction.response.send_message("⌨️ Đã gửi phím `q`", ephemeral=True)

        btn_quit.callback = on_quit_click
        self.add_item(btn_quit)

        btn_kill = Button(label="🛑 Dừng (Ctrl+C)", style=discord.ButtonStyle.danger, row=0)

        async def on_kill_click(interaction: discord.Interaction):
            await self.session.kill()
            btn_kill.disabled = True
            btn_kill.label = "Đã dừng..."
            await interaction.response.edit_message(view=self)

        btn_kill.callback = on_kill_click
        self.add_item(btn_kill)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _is_admin(interaction.user.id, self.bot):
            await interaction.response.send_message("❌ Bạn không có quyền điều khiển tiến trình này!", ephemeral=True)
            return False
        return True


class ProcessSession:
    def __init__(
        self,
        session_id: str,
        command: str,
        bot,
        channel: Optional[discord.abc.Messageable],
        show_console: bool = DEFAULT_SHOW_CONSOLE
    ):
        self.session_id = session_id
        self.command = command
        self.bot = bot
        self.channel = channel
        self.show_console = show_console
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.terminal = VirtualTerminal(cols=DEFAULT_COLS, max_rows=500)
        self.start_time = time.time()
        self.end_time: Optional[float] = None
        self.message: Optional[discord.Message] = None
        self.view: Optional[InteractiveProcessView] = None
        self.updater_task: Optional[asyncio.Task] = None
        self.done_event = asyncio.Event()
        self.is_terminated = False
        self.master_fd: Optional[int] = None

    def get_full_output(self) -> str:
        return self.terminal.render()

    def get_display_text(self) -> str:
        elapsed = int((self.end_time or time.time()) - self.start_time)
        header_status = "⚙️ **Đang chạy (PTY)**" if not self.done_event.is_set() else (
            "🛑 **Đã dừng (Ctrl+C)**" if self.is_terminated else f"✅ **Hoàn thành** (Exit code: `{self.proc.returncode if self.proc else '?'}`)"
        )

        if not self.show_console:
            # Compact view
            note = "*(Console đang ẩn để giữ kênh gọn gàng. Bấm 'Xem Console' bên dưới để xem)*" if not self.done_event.is_set() else "*(Tiến trình đã kết thúc)*"
            return (
                f"{header_status}: `{self.command[:80]}`\n"
                f"⏱️ Thời gian: `{elapsed}s` | ID: `{self.session_id}`\n"
                f"{note}"
            )

        rendered = self.terminal.render(max_lines=25).strip()
        max_box = 1500
        if len(rendered) > max_box:
            content = "...\n" + rendered[-max_box:]
        elif rendered:
            content = rendered
        else:
            content = "[Đang chờ dữ liệu đầu ra...]"

        return (
            f"{header_status}: `{self.command[:80]}`\n"
            f"⏱️ Thời gian: `{elapsed}s` | ID: `{self.session_id}`\n"
            f"```bash\n{content}\n```"
        )

    async def update_discord_message(self, final: bool = False):
        if not self.message:
            return
        try:
            view = self.view
            if view:
                view.rebuild_buttons()
                if final:
                    for child in view.children:
                        if isinstance(child, Button) and "Dừng" in getattr(child, "label", ""):
                            child.disabled = True

            await self.message.edit(content=self.get_display_text(), view=view if not final else None)
        except discord.HTTPException:
            pass

    async def _updater_loop(self):
        while not self.done_event.is_set():
            await asyncio.sleep(UPDATE_INTERVAL)
            await self.update_discord_message(final=False)

    async def send_keys(self, keys: str, send_enter: bool = False):
        key_upper = keys.strip().upper()
        payload = KEY_ALIASES.get(key_upper, keys)
        if send_enter and not payload.endswith(("\r", "\n")):
            payload += "\r"

        if self.master_fd is not None:
            try:
                os.write(self.master_fd, payload.encode("utf-8"))
            except OSError as e:
                self.terminal.write(f"\n[Lỗi gửi PTY keystroke: {e}]\n")
        elif self.proc and self.proc.stdin and not self.proc.stdin.is_closing():
            try:
                self.proc.stdin.write(payload.encode("utf-8"))
                await self.proc.stdin.drain()
            except Exception as e:
                self.terminal.write(f"\n[Lỗi gửi stdin: {e}]\n")

    async def kill(self):
        self.is_terminated = True
        if self.master_fd is not None:
            try:
                os.write(self.master_fd, b"\x03")
            except OSError:
                pass

        if self.proc and self.proc.returncode is None:
            try:
                self.proc.send_signal(signal.SIGINT)
                await asyncio.sleep(1.0)
                if self.proc.returncode is None:
                    self.proc.kill()
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass

    async def start(self, initial_password: Optional[str] = None):
        cmd = self.command
        if initial_password:
            cmd = _prepare_sudo_command(cmd)

        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLUMNS"] = str(DEFAULT_COLS)
        env["LINES"] = str(DEFAULT_ROWS)
        env["PAGER"] = "cat"
        env["GIT_PAGER"] = "cat"
        env["SYSTEMD_PAGER"] = "cat"

        use_pty = True
        try:
            master_fd, slave_fd = pty.openpty()
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", DEFAULT_ROWS, DEFAULT_COLS, 0, 0))
            self.master_fd = master_fd
        except Exception:
            use_pty = False
            self.master_fd = None

        if use_pty:
            self.proc = await asyncio.create_subprocess_shell(
                cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=env,
                close_fds=True
            )
            os.close(slave_fd)
        else:
            self.proc = await asyncio.create_subprocess_shell(
                cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )

        if initial_password:
            await asyncio.sleep(0.1)
            await self.send_keys(initial_password + "\r")

        if self.channel:
            self.view = InteractiveProcessView(self, self.bot)
            try:
                self.message = await self.channel.send(self.get_display_text(), view=self.view)
            except discord.HTTPException:
                pass

        self.updater_task = asyncio.create_task(self._updater_loop())
        loop = asyncio.get_running_loop()

        if use_pty and self.master_fd is not None:
            def on_pty_read():
                try:
                    data = os.read(self.master_fd, 2048)
                    if not data:
                        loop.remove_reader(self.master_fd)
                        return
                    self.terminal.write(data.decode("utf-8", errors="replace"))
                except OSError:
                    try:
                        loop.remove_reader(self.master_fd)
                    except Exception:
                        pass

            loop.add_reader(self.master_fd, on_pty_read)
            await self.proc.wait()
            await asyncio.sleep(0.2)
            try:
                loop.remove_reader(self.master_fd)
            except Exception:
                pass
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        else:
            async def read_pipe(stream):
                while True:
                    chunk = await stream.read(1024)
                    if not chunk:
                        break
                    self.terminal.write(chunk.decode("utf-8", errors="replace"))

            t1 = asyncio.create_task(read_pipe(self.proc.stdout))
            t2 = asyncio.create_task(read_pipe(self.proc.stderr))
            await asyncio.gather(t1, t2)
            await self.proc.wait()

        self.end_time = time.time()
        self.done_event.set()

        if self.updater_task:
            self.updater_task.cancel()

        full_out = self.get_full_output().lower()
        if "incorrect password attempt" in full_out or "password is required" in full_out:
            _clear_cached_sudo_password()

        await self.update_discord_message(final=True)


# ---------------------------------------------------------------------------
# AI Tools
# ---------------------------------------------------------------------------
@tool(
    name="run_shell_command",
    description="Execute a shell command inside a virtual PTY terminal. Set show_console=True if the user explicitly asks to display the full console box in the channel message."
)
async def run_shell_command(
    command: str,
    context: ToolContext,
    timeout_seconds: int = 30,
    show_console: bool = DEFAULT_SHOW_CONSOLE
) -> str:
    """
    Executes a shell command in an emulated TTY.
    Cleans ANSI/Unicode escapes, supports sudo, and provides a Discord controller.
    """
    if not context.author or not _is_admin(context.author.id, context.bot):
        user_name = context.author.name if context.author else "Unknown"
        user_id = context.author.id if context.author else 0
        return f"Permission Denied: User '{user_name}' (ID: {user_id}) is not in adminIds."

    sudo_password = None

    if _has_sudo(command):
        try:
            test_proc = await asyncio.create_subprocess_shell(
                "sudo -n true",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await test_proc.wait()
            can_run_passwordless = (test_proc.returncode == 0)
        except Exception:
            can_run_passwordless = False

        if not can_run_passwordless:
            cached_pw = _get_cached_sudo_password()
            if cached_pw:
                sudo_password = cached_pw
            else:
                sudo_password = await _request_sudo_password(context, timeout=60.0)
                if not sudo_password:
                    return "Cancelled: Sudo password authentication was not completed or timed out."
                _set_cached_sudo_password(sudo_password)

    session_id = f"proc_{int(time.time() * 1000) % 1000000}"
    session = ProcessSession(
        session_id=session_id,
        command=command,
        bot=context.bot,
        channel=context.channel,
        show_console=show_console
    )
    _active_sessions[session_id] = session

    run_task = asyncio.create_task(session.start(initial_password=sudo_password))

    try:
        await asyncio.wait_for(asyncio.shield(run_task), timeout=float(timeout_seconds))
        output = session.get_full_output()
        exit_code = session.proc.returncode if session.proc else 0
        _active_sessions.pop(session_id, None)

        if len(output) > MAX_OUTPUT_LENGTH:
            output = output[:MAX_OUTPUT_LENGTH] + "\n... [Output truncated]"
        return f"Process finished (exit code {exit_code}):\n{output}" if output else f"Process finished with exit code {exit_code} (no output)."

    except asyncio.TimeoutError:
        status_note = "Console is currently displayed on the message." if show_console else "Console is hidden (click 'Xem Console' button on the message for temporary popup)."
        return (
            f"ℹ️ Command `{command[:60]}` is running in the background (ID: `{session_id}`).\n"
            f"{status_note}"
        )


@tool(
    name="toggle_process_console",
    description="Force show or hide the live console output on the Discord channel message for an active process."
)
async def toggle_process_console(
    show: bool,
    context: ToolContext,
    session_id: str = ""
) -> str:
    """Forces showing or hiding the console codeblock on the channel message."""
    if not context.author or not _is_admin(context.author.id, context.bot):
        return "Permission Denied: Only administrators can control the console display."

    if session_id and session_id in _active_sessions:
        session = _active_sessions[session_id]
    elif _active_sessions:
        session = list(_active_sessions.values())[-1]
    else:
        return "No running terminal session found."

    session.show_console = show
    await session.update_discord_message()
    state = "hiển thị" if show else "ẩn"
    return f"Đã {state} console cho tiến trình `{session.command}` (ID: `{session.session_id}`)."


@tool(
    name="send_terminal_keys",
    description="Send keystrokes (e.g. '1', 'q', 'd', 'UP', 'DOWN', 'ENTER') to a running TUI app (e.g. top, btop, less) and returns the updated screen."
)
async def send_terminal_keys(
    keys: str,
    context: ToolContext,
    session_id: str = "",
    wait_seconds: float = 1.0,
    send_enter: bool = False
) -> str:
    """
    Sends keys (e.g. '1' to toggle CPUs in top, 'q' to quit, 'd' to change delay)
    to the active terminal session and returns the freshly updated screen view.
    """
    if not context.author or not _is_admin(context.author.id, context.bot):
        return "Permission Denied: Only administrators can control running terminal sessions."

    if session_id and session_id in _active_sessions:
        session = _active_sessions[session_id]
    elif _active_sessions:
        session = list(_active_sessions.values())[-1]
    else:
        return "No running terminal session found to send keys to."

    if session.done_event.is_set():
        return f"Process `{session.command}` has already terminated."

    await session.send_keys(keys, send_enter=send_enter)
    await asyncio.sleep(max(0.2, min(wait_seconds, 5.0)))
    await session.update_discord_message()

    current_screen = session.terminal.render(max_lines=25)
    return (
        f"Sent keys '{keys}' to process `{session.command}` (ID: `{session.session_id}`).\n"
        f"Current Screen Output:\n```bash\n{current_screen}\n```"
    )


@tool(
    name="kill_running_command",
    description="Terminate a running background shell process by session ID or stops the latest active process."
)
async def kill_running_command(context: ToolContext, session_id: str = "") -> str:
    """Terminates an ongoing background command."""
    if not context.author or not _is_admin(context.author.id, context.bot):
        return "Permission Denied: Only administrators can stop running commands."

    if session_id and session_id in _active_sessions:
        target_session = _active_sessions[session_id]
    elif _active_sessions:
        target_session = list(_active_sessions.values())[-1]
    else:
        return "No running background commands found."

    await target_session.kill()
    _active_sessions.pop(target_session.session_id, None)
    return f"Terminated process `{target_session.command}` (ID: `{target_session.session_id}`)."


# ---------------------------------------------------------------------------
# Slash Commands for Managing Process Console
# ---------------------------------------------------------------------------
def on_load(bot):
    if not bot:
        return

    @app_commands.command(name="console", description="Bật/tắt hoặc xem console của tiến trình shell đang chạy")
    @app_commands.describe(
        action="Chọn hành động hiển thị console",
        session_id="ID tiến trình (để trống để chọn tiến trình gần nhất)"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Bật/Tắt hiển thị console trên kênh (Toggle)", value="toggle"),
        app_commands.Choice(name="Bắt buộc hiện console trên kênh (Force Show)", value="show"),
        app_commands.Choice(name="Bắt buộc ẩn console trên kênh (Force Hide)", value="hide"),
        app_commands.Choice(name="Xem console tạm thời (Ephemeral View)", value="view")
    ])
    async def console_slash(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        session_id: str = ""
    ):
        if not _is_admin(interaction.user.id, bot):
            await interaction.response.send_message("❌ Bạn không có quyền sử dụng lệnh này!", ephemeral=True)
            return

        target_session = None
        if session_id and session_id in _active_sessions:
            target_session = _active_sessions[session_id]
        elif _active_sessions:
            target_session = list(_active_sessions.values())[-1]

        if not target_session:
            await interaction.response.send_message("ℹ️ Hiện không có tiến trình shell nào đang chạy.", ephemeral=True)
            return

        if action.value == "toggle":
            target_session.show_console = not target_session.show_console
            await target_session.update_discord_message()
            state = "HIỆN" if target_session.show_console else "ẨN"
            await interaction.response.send_message(f"✅ Đã chuyển trạng thái console của `{target_session.session_id}` sang **{state}**.", ephemeral=True)
        elif action.value == "show":
            target_session.show_console = True
            await target_session.update_discord_message()
            await interaction.response.send_message(f"✅ Đã bật hiển thị console của `{target_session.session_id}` trên kênh.", ephemeral=True)
        elif action.value == "hide":
            target_session.show_console = False
            await target_session.update_discord_message()
            await interaction.response.send_message(f"✅ Đã ẩn console của `{target_session.session_id}` khỏi tin nhắn kênh.", ephemeral=True)
        elif action.value == "view":
            output = target_session.terminal.render(max_lines=25).strip() or "[Chưa có dữ liệu]"
            elapsed = int((target_session.end_time or time.time()) - target_session.start_time)
            header = "⚙️ Đang chạy (PTY)" if not target_session.done_event.is_set() else "✅ Đã hoàn thành"
            content = (
                f"🖥️ **Console tạm thời ({header}):** `{target_session.command[:70]}`\n"
                f"⏱️ Thời gian: `{elapsed}s` | ID: `{target_session.session_id}`\n"
                f"```bash\n{output}\n```"
            )
            ephemeral_view = EphemeralConsoleView(target_session, bot)
            await interaction.response.send_message(content=content, view=ephemeral_view, ephemeral=True)

    try:
        bot.tree.remove_command("console")
    except Exception:
        pass

    bot.tree.add_command(console_slash)
    bot._console_command = console_slash

    # Direct chat command listener: !console [show|hide|toggle|view]
    async def console_message_listener(message: discord.Message):
        if not message.author or not _is_admin(message.author.id, bot):
            return
        content = message.content.strip().lower()
        if not content.startswith(("!console", "!cs")):
            return

        parts = content.split()
        subcmd = parts[1] if len(parts) > 1 else "toggle"

        target_session = list(_active_sessions.values())[-1] if _active_sessions else None
        if not target_session:
            await message.channel.send("ℹ️ Hiện không có tiến trình shell nào đang chạy.")
            return

        if subcmd in ("show", "on", "1", "hien", "hiện"):
            target_session.show_console = True
            await target_session.update_discord_message()
            await message.channel.send(f"🖥️ Đã **bật hiển thị** console trên tin nhắn kênh.")
        elif subcmd in ("hide", "off", "0", "an", "ẩn"):
            target_session.show_console = False
            await target_session.update_discord_message()
            await message.channel.send(f"🙈 Đã **ẩn** console khỏi tin nhắn kênh.")
        elif subcmd in ("toggle", "t"):
            target_session.show_console = not target_session.show_console
            await target_session.update_discord_message()
            state = "bật hiển thị" if target_session.show_console else "ẩn"
            await message.channel.send(f"🔄 Đã chuyển console sang **{state}**.")
        elif subcmd in ("view", "xem"):
            output = target_session.terminal.render(max_lines=25).strip() or "[Chưa có dữ liệu]"
            elapsed = int((target_session.end_time or time.time()) - target_session.start_time)
            header = "⚙️ Đang chạy (PTY)" if not target_session.done_event.is_set() else "✅ Đã hoàn thành"
            content_msg = (
                f"🖥️ **Console ({header}):** `{target_session.command[:70]}`\n"
                f"⏱️ Thời gian: `{elapsed}s` | ID: `{target_session.session_id}`\n"
                f"```bash\n{output}\n```"
            )
            await message.channel.send(content_msg)

    if hasattr(bot, "_console_listener"):
        try:
            bot.remove_listener(bot._console_listener, "on_message")
        except Exception:
            pass

    bot.add_listener(console_message_listener, "on_message")
    bot._console_listener = console_message_listener


def on_unload(bot):
    if bot and hasattr(bot, "_console_command"):
        try:
            bot.tree.remove_command("console")
        except Exception:
            pass
        del bot._console_command

    if bot and hasattr(bot, "_console_listener"):
        try:
            bot.remove_listener(bot._console_listener, "on_message")
        except Exception:
            pass
        del bot._console_listener
