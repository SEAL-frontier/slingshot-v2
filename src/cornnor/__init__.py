from collections import OrderedDict
from discord import app_commands, Attachment, Embed, Game, HTTPException, Intents, Interaction, User
from discord.ext import commands, tasks
from typing import List

from ._models import CornnorConfig
from ._utils import *
from .modules import ExtensionManager, ToolContext, tool

import asyncio, logging, os, random

# Logging formatter, don't touch.
class CornorLogFormatter(logging.Formatter):
    LIGHT_YELLOW = "\033[93m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    RESET = "\033[0m"
    def format(self, record):
        if record.threadName and record.threadName != "MainThread":
            thread_part = record.threadName
        else:
            thread_part = "<init>"
        log_fmt = (
            f"{self.LIGHT_YELLOW}[%(asctime)s]{self.RESET} "
            f"{self.CYAN}[%(name)s/%(levelname)s]{self.RESET} "
            f"{self.GREEN}({thread_part}){self.RESET} "
            f"%(message)s"
        )
        formatter = logging.Formatter(log_fmt, datefmt="%Y/%m/%d %H:%M:%S")
        return formatter.format(record)
# ---------------------------------------------------------------------------------------------


class Cornnor(commands.Bot):
    '''
    Connor, but 🌽
    '''
    def __init__(self, config: CornnorConfig):
        # system prompt file is required externally
        if not os.path.exists("system_prompt.txt"):
            raise RuntimeError("System prompt does not specified. Create \"system_prompt.txt\" now.")
            
        # logging stuffs (cornnor)
        self.logger = logging.getLogger("cornnor")
        self.logger.propagate = False 
        self.logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        handler.setFormatter(CornorLogFormatter())
        self.logger.addHandler(handler)

        # logging stuffs (global)
        root_logger = logging.getLogger()
        if root_logger.hasHandlers():
            root_logger.handlers.clear()
        root_logger.setLevel(logging.INFO)
        handler2 = logging.StreamHandler()
        handler2.setFormatter(CornorLogFormatter())
        root_logger.addHandler(handler2)

        # bot core init
        intents = Intents.default()
        intents.typing = False
        intents.presences = True
        intents.messages = True
        intents.message_content = True
        super().__init__(command_prefix="/", intents=intents)

        # stuffs
        self.discord_typing_lock = asyncio.Lock()
        self.image_cache = OrderedDict()
        self.system_prompt = open("system_prompt.txt", "r").read()
        self.config = config
        self.STATUS_FILE = "games.json"
        self.MEMORY_FILE = "chat_memory.json"
        self.games = JsonUtils.loadJson(self.STATUS_FILE, [{"title": "?"}])
        self.chat_memory = JsonUtils.loadJson(self.MEMORY_FILE, {})

        # modules / extensions
        ext_dir = getattr(self.config, "extensionsDir", "extensions")
        self.extension_manager = ExtensionManager(bot=self, extensions_dir=ext_dir)
        self.extension_manager.load_all()


    @tasks.loop(hours=1)
    async def change_game_status(self):
        game = random.choice(self.games)
        self.current_game = game["title"]
        await self.change_presence(activity=Game(name=game["title"]))
        
    @change_game_status.before_loop
    async def before_status(self):
        await self.wait_until_ready()

    async def typing_indicator(self, channel):
        async with self.discord_typing_lock:
            await channel.typing()

    async def on_ready(self):
        self.logger.info(f"Logged in as {self.user}")
        try:
            synced = await self.tree.sync()
            self.logger.info(f"✅ Slash commands synced: {len(synced)}")
        except Exception as e:
            self.logger.error(f"❌ Sync error: {e}")
        

    async def setup_hook(self):
        # Start background tasks
        if not self.change_game_status.is_running():
            self.change_game_status.start()

        @self.command()
        async def system(ctx, *, input: str):
            async with ctx.typing():
                MemoryUtils.updateMemory(
                    self.MEMORY_FILE,
                    self.chat_memory,
                    self.config.maxHistory,
                    str(ctx.author.id), 
                    ctx.author.display_name, 
                    input, 
                    "user"
                )
                response = await ChatUtils.responseStream(
                    self.config.apiEndpoint, 
                    self.config.apiKey, 
                    self.system_prompt,
                    self.config.modelName,
                    self.chat_memory,
                    ctx.message, 
                    ctx.author.display_name,
                    ctx.channel, 
                    user_text_override=input,
                    extension_manager=self.extension_manager,
                    bot=self
                )
                MemoryUtils.updateMemory(
                    self.MEMORY_FILE,
                    self.chat_memory,
                    self.config.maxHistory,
                    str(ctx.author.id), 
                    ctx.author.display_name, 
                    response, 
                    "assistant"
                )
                await ctx.send(response)

        @self.tree.command(name="purgememory", description="Xóa bộ nhớ trò chuyện với bot")
        @app_commands.describe(
            scope="Chọn phạm vi cần xóa",
            target="Người dùng cụ thể (chỉ khi chọn 'user', admin only)"
        )
        @app_commands.choices(scope=[
            app_commands.Choice(name="Chỉ mình tôi", value="me"),
            app_commands.Choice(name="Người dùng cụ thể", value="user"),
            app_commands.Choice(name="Toàn bộ (admin only, DO NOT USE)", value="all")
        ])
        async def purge(
            interaction: Interaction,
            scope: app_commands.Choice[str],
            target: User | None
        ):
            if scope.value == "me":
                user_id = str(interaction.user.id)
                if user_id in self.chat_memory:
                    del self.chat_memory[user_id]
                    JsonUtils.saveJson(self.MEMORY_FILE, self.chat_memory)
                    await interaction.response.send_message("🧹 Đã xóa bộ nhớ của bạn với bot.", ephemeral=True)
                else:
                    await interaction.response.send_message("ℹ️ Bạn không có dữ liệu bộ nhớ nào để xóa.", ephemeral=True)

            elif scope.value == "user":
                if interaction.user.id not in self.config.adminIds:
                    await interaction.response.send_message("❌ Bạn không có quyền dùng tùy chọn này!", ephemeral=True)
                    return
                if target is None:
                    await interaction.response.send_message("❌ Bạn phải chọn một người dùng để xóa bộ nhớ!", ephemeral=True)
                    return

                user_id = str(target.id)
                if user_id in self.chat_memory:
                    del self.chat_memory[user_id]
                    JsonUtils.saveJson(self.MEMORY_FILE, self.chat_memory)
                    await interaction.response.send_message(f"🧹 Đã xóa bộ nhớ của {target.mention}.", ephemeral=True)
                else:
                    await interaction.response.send_message(f"ℹ️ {target.mention} không có dữ liệu bộ nhớ nào.", ephemeral=True)

            elif scope.value == "all":
                if interaction.user.id not in self.config.adminIds:
                    await interaction.response.send_message("❌ Bạn không có quyền dùng tùy chọn này!", ephemeral=True)
                    return

                self.chat_memory = {}
                JsonUtils.saveJson(self.MEMORY_FILE, self.chat_memory)
                await interaction.response.send_message("🧹 Đã xóa toàn bộ bộ nhớ của bot.", ephemeral=True)

        ext_group = app_commands.Group(name="extensions", description="Quản lý các module mở rộng cho AI")

        @ext_group.command(name="list", description="Xem danh sách các module và công cụ AI đang tải")
        async def list_extensions(interaction: Interaction):
            loaded = self.extension_manager.loaded_modules
            tools = self.extension_manager.tools
            if not loaded:
                await interaction.response.send_message("ℹ️ Hiện không có extension nào được tải.", ephemeral=True)
                return

            embed = Embed(title="🧩 AI Extensions & Tools", color=0x3498db)
            for ext_name in loaded.keys():
                ext_tools = [f"`{t.name}`: {t.description}" for t in tools.values() if t.extension_name == ext_name]
                tools_str = "\n".join(ext_tools) if ext_tools else "*Không có tools*"
                embed.add_field(name=f"📦 {ext_name}", value=tools_str, inline=False)

            await interaction.response.send_message(embed=embed, ephemeral=True)

        @ext_group.command(name="reload", description="Tải lại toàn bộ extensions (Admin only)")
        async def reload_extensions(interaction: Interaction):
            if interaction.user.id not in self.config.adminIds:
                await interaction.response.send_message("❌ Bạn không có quyền dùng tùy chọn này!", ephemeral=True)
                return

            results = self.extension_manager.reload_all()
            success_count = sum(1 for v in results.values() if v)
            tool_count = len(self.extension_manager.tools)
            await interaction.response.send_message(
                f"🔄 Đã tải lại extensions: **{success_count}/{len(results)}** thành công ({tool_count} tools hoạt động).",
                ephemeral=True
            )

        self.tree.add_command(ext_group)

    async def on_message(self, message):
        assert self.user != None
        if message.author.id == self.user.id or not message.guild:
            return
        if message.mention_everyone or message.role_mentions:
            return
        image_data_urls = []
        image_descriptions = []

        if message.attachments:
            for attachment in message.attachments:
                if AttachmentUtils.isImage(attachment):
                    data_url = await AttachmentUtils.attachmentToUrl(
                        self.image_cache, 
                        self.config.maxImageCache, 
                        self.config.maxImageSizeByte, 
                        attachment
                    )
                    if data_url:
                        image_data_urls.append(data_url)
                        image_descriptions.append(attachment.filename or "Hình ảnh")

        if self.user.mentioned_in(message) and not message.reference:
            async with message.channel.typing():
                MemoryUtils.updateMemory(
                    self.MEMORY_FILE,
                    self.chat_memory,
                    self.config.maxHistory,
                    str(message.author.id),
                    message.author.display_name,
                    MemoryUtils.buildMemoryMessage(message.content, image_descriptions),
                    "user"
                )
                response = await ChatUtils.responseStream(
                    self.config.apiEndpoint, 
                    self.config.apiKey, 
                    self.system_prompt,
                    self.config.modelName,
                    self.chat_memory,
                    message, 
                    message.author.display_name, 
                    message.channel, 
                    image_data_urls=image_data_urls,
                    extension_manager=self.extension_manager,
                    bot=self
                )

                MemoryUtils.updateMemory(
                    self.MEMORY_FILE,
                    self.chat_memory,
                    self.config.maxHistory,
                    str(message.author.id), 
                    message.author.display_name, 
                    response, 
                    "assistant"
                )
            return

        if message.reference:
            replied_message = await message.channel.fetch_message(message.reference.message_id)
            original_author = replied_message.author
            original_content = replied_message.content
            user_content = message.content.replace(f"<@{self.user.id}>", "").strip()

            if self.user.mentioned_in(message) or triggerMatched(message.content, self.config.triggerWords):
                async with message.channel.typing():
                    if original_author.id == self.user.id:
                        MemoryUtils.updateMemory(
                            self.MEMORY_FILE,
                            self.chat_memory,
                            self.config.maxHistory,
                            str(message.author.id),
                            message.author.display_name,
                            MemoryUtils.buildMemoryMessage(user_content, image_descriptions),
                            "user"
                        )
                    else:
                        combined_prompt = (
                            f"Original message from {original_author.display_name}: {original_content}\n"
                            f"Reply from {message.author.display_name}: {user_content}"
                        )
                        MemoryUtils.updateMemory(
                            self.MEMORY_FILE,
                            self.chat_memory,
                            self.config.maxHistory,
                            str(message.author.id),
                            message.author.display_name,
                            MemoryUtils.buildMemoryMessage(combined_prompt, image_descriptions),
                            "user"
                        )

                    response = await ChatUtils.responseStream(
                        self.config.apiEndpoint, 
                        self.config.apiKey, 
                        self.system_prompt,
                        self.config.modelName,
                        self.chat_memory,
                        message,
                        message.author.display_name,
                        message.channel,
                        image_data_urls=image_data_urls,
                        user_text_override=user_content if original_author.id == self.user.id else combined_prompt,
                        extension_manager=self.extension_manager,
                        bot=self
                    )

                    MemoryUtils.updateMemory(
                        self.MEMORY_FILE,
                        self.chat_memory,
                        self.config.maxHistory,
                        str(message.author.id), 
                        message.author.display_name, 
                        response, 
                        "assistant"
                    )
                return

        elif triggerMatched(message.content, self.config.triggerWords):
            async with message.channel.typing():
                MemoryUtils.updateMemory(
                    self.MEMORY_FILE,
                    self.chat_memory,
                    self.config.maxHistory,
                    str(message.author.id),
                    message.author.display_name,
                    MemoryUtils.buildMemoryMessage(message.content, image_descriptions),
                    "user"
                )
                response = await ChatUtils.responseStream(
                    self.config.apiEndpoint, 
                    self.config.apiKey, 
                    self.system_prompt,
                    self.config.modelName,
                    self.chat_memory,
                    message, 
                    message.author.display_name, 
                    message.channel, 
                    image_data_urls=image_data_urls,
                    extension_manager=self.extension_manager,
                    bot=self
                )
                MemoryUtils.updateMemory(
                    self.MEMORY_FILE,
                    self.chat_memory,
                    self.config.maxHistory,
                    str(message.author.id), 
                    message.author.display_name, 
                    response, 
                    "assistant"
                )
            return

        await self.process_commands(message)

    # ok ok ok 
    def run(self, *args, **kwargs):
        super().run(self.config.botToken, log_formatter=CornorLogFormatter(), log_handler=None)

    

__all__ = [
    "Cornnor",
    "CornnorConfig"
]