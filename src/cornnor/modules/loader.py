import importlib.util
import inspect
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional
import discord
from discord.ext import commands

from .tool import ToolContext, ToolDefinition, tool


logger = logging.getLogger("cornnor.modules")


class ExtensionManager:
    """
    Manages discovering, dynamically loading, reloading, and executing
    extensions and their registered AI tools.
    """
    def __init__(self, bot: Optional[commands.Bot] = None, extensions_dir: str = "extensions"):
        self.bot = bot
        self.extensions_dir = extensions_dir
        self.loaded_modules: Dict[str, Any] = {}
        self.tools: Dict[str, ToolDefinition] = {}

    def ensure_dir(self) -> None:
        if not os.path.exists(self.extensions_dir):
            os.makedirs(self.extensions_dir, exist_ok=True)

    def load_all(self) -> Dict[str, bool]:
        """Scans the extensions directory and loads all .py extension modules."""
        self.ensure_dir()
        results = {}
        for entry in sorted(os.listdir(self.extensions_dir)):
            if entry.startswith(("_", ".")):
                continue
            full_path = os.path.join(self.extensions_dir, entry)
            if entry.endswith(".py") and os.path.isfile(full_path):
                ext_name = entry[:-3]
                results[ext_name] = self.load_extension(ext_name)
            elif os.path.isdir(full_path) and os.path.exists(os.path.join(full_path, "__init__.py")):
                results[entry] = self.load_extension(entry)
        logger.info(f"Loaded {len(self.loaded_modules)} extension(s) with {len(self.tools)} tool(s).")
        return results

    def _resolve_extension_path(self, name: str) -> Optional[str]:
        if os.path.isabs(name) and os.path.exists(name):
            return name
        candidate_py = os.path.join(self.extensions_dir, f"{name}.py")
        if os.path.isfile(candidate_py):
            return candidate_py
        candidate_dir = os.path.join(self.extensions_dir, name, "__init__.py")
        if os.path.isfile(candidate_dir):
            return candidate_dir
        return None

    def load_extension(self, name: str) -> bool:
        """Loads a single extension by name or file path."""
        file_path = self._resolve_extension_path(name)
        if not file_path:
            logger.error(f"Cannot find extension '{name}' in '{self.extensions_dir}'.")
            return False

        module_name = f"cornnor_ext_{os.path.splitext(os.path.basename(file_path))[0]}"
        try:
            if name in self.loaded_modules:
                self.unload_extension(name)

            spec = importlib.util.spec_from_file_location(module_name, file_path)
            if spec is None or spec.loader is None:
                logger.error(f"Failed to create module spec for '{name}'.")
                return False

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            # Register tools
            registered_tools = []
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if callable(attr) and getattr(attr, "_is_ai_tool", False):
                    tool_name = getattr(attr, "_tool_name", None) or attr_name
                    tool_desc = getattr(attr, "_tool_description", None)
                    tool_def = ToolDefinition(attr, name=tool_name, description=tool_desc, extension_name=name)
                    self.tools[tool_name] = tool_def
                    registered_tools.append(tool_name)

            # Run lifecycle hook if available
            setup_func = getattr(module, "on_load", getattr(module, "setup", None))
            if callable(setup_func):
                if inspect.iscoroutinefunction(setup_func):
                    # Will be run if in loop, or schedule
                    if self.bot and self.bot.loop and self.bot.loop.is_running():
                        self.bot.loop.create_task(setup_func(self.bot))
                else:
                    setup_func(self.bot)

            self.loaded_modules[name] = module
            logger.info(f"Loaded extension '{name}' with tools: {registered_tools}")
            return True
        except Exception as e:
            logger.exception(f"Error loading extension '{name}': {e}")
            return False

    def unload_extension(self, name: str) -> bool:
        """Unloads an extension, deregisters its tools, and calls on_unload."""
        module = self.loaded_modules.pop(name, None)
        if not module:
            return False

        # Run teardown hook
        teardown_func = getattr(module, "on_unload", getattr(module, "teardown", None))
        if callable(teardown_func):
            try:
                if inspect.iscoroutinefunction(teardown_func):
                    if self.bot and self.bot.loop and self.bot.loop.is_running():
                        self.bot.loop.create_task(teardown_func(self.bot))
                else:
                    teardown_func(self.bot)
            except Exception as e:
                logger.warning(f"Error in on_unload hook for '{name}': {e}")

        # Remove tools from this extension
        tools_to_remove = [k for k, v in self.tools.items() if v.extension_name == name]
        for t in tools_to_remove:
            del self.tools[t]

        return True

    def reload_extension(self, name: str) -> bool:
        """Reloads a specific extension."""
        self.unload_extension(name)
        return self.load_extension(name)

    def reload_all(self) -> Dict[str, bool]:
        """Reloads all active and newly discovered extensions."""
        current = list(self.loaded_modules.keys())
        for ext in current:
            self.unload_extension(ext)
        return self.load_all()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Returns OpenAI-compatible function calling schemas for all active tools."""
        return [tool_def.schema for tool_def in self.tools.values()]

    def get_system_prompt_addon(self) -> str:
        """Aggregates any dynamic system prompt additions defined in extensions."""
        addons = []
        for name, module in self.loaded_modules.items():
            addon = getattr(module, "SYSTEM_PROMPT", getattr(module, "system_prompt_addon", None))
            if callable(addon):
                try:
                    res = addon()
                    if res:
                        addons.append(f"[{name}]\n{res}")
                except Exception as e:
                    logger.warning(f"Error getting system prompt addon from '{name}': {e}")
            elif isinstance(addon, str) and addon.strip():
                addons.append(f"[{name}]\n{addon.strip()}")
        return "\n\n".join(addons)

    async def execute_tool(
        self,
        name: str,
        arguments: Dict[str, Any],
        context: Optional[ToolContext] = None
    ) -> str:
        """Executes a registered tool by name with arguments and context."""
        if name not in self.tools:
            return f"Error: Tool '{name}' not found."

        tool_def = self.tools[name]
        try:
            result = await tool_def.execute(arguments, context)
            if isinstance(result, (dict, list)):
                return json.dumps(result, ensure_ascii=False)
            return str(result)
        except Exception as e:
            logger.exception(f"Error executing tool '{name}': {e}")
            return f"Error executing tool '{name}': {type(e).__name__}: {str(e)}"
