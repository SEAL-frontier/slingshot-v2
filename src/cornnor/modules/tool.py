import inspect
import json
import typing
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, get_type_hints
import discord
from discord.ext import commands


@dataclass
class ToolContext:
    """Runtime context provided to tools when executed."""
    bot: Optional[commands.Bot] = None
    channel: Optional[discord.abc.Messageable] = None
    message: Optional[discord.Message] = None
    author: Optional[discord.abc.User] = None
    extra: Dict[str, Any] = field(default_factory=dict)


class ToolDefinition:
    def __init__(
        self,
        func: Callable,
        name: Optional[str] = None,
        description: Optional[str] = None,
        extension_name: str = ""
    ):
        self.func = func
        self.name = name or func.__name__
        self.description = description or inspect.getdoc(func) or f"Execute {self.name}"
        self.extension_name = extension_name
        self.is_async = inspect.iscoroutinefunction(func)
        self.schema = self._generate_schema()

    def _python_type_to_json_type(self, py_type: Any) -> Dict[str, Any]:
        """Maps Python types to JSON Schema types."""
        origin = typing.get_origin(py_type)
        if origin is not None:
            if origin in (list, List):
                args = typing.get_args(py_type)
                item_type = self._python_type_to_json_type(args[0]) if args else {"type": "string"}
                return {"type": "array", "items": item_type}
            if origin in (dict, Dict):
                return {"type": "object"}
            if origin is typing.Union:
                args = [a for a in typing.get_args(py_type) if a is not type(None)]
                if len(args) == 1:
                    return self._python_type_to_json_type(args[0])
                return {"type": "string"}

        if py_type in (int,):
            return {"type": "integer"}
        if py_type in (float,):
            return {"type": "number"}
        if py_type in (bool,):
            return {"type": "boolean"}
        if py_type in (dict,):
            return {"type": "object"}
        if py_type in (list,):
            return {"type": "array", "items": {"type": "string"}}
        return {"type": "string"}

    def _is_injected_param(self, name: str, param_type: Any) -> bool:
        """Determines if a parameter should be injected from context rather than LLM."""
        if name in {"context", "ctx", "_context", "bot", "channel", "message", "author"}:
            return True
        if param_type in (
            ToolContext,
            discord.Message,
            discord.abc.Messageable,
            discord.TextChannel,
            discord.User,
            discord.Member,
            discord.abc.User,
            commands.Bot
        ):
            return True
        return False

    def _generate_schema(self) -> Dict[str, Any]:
        sig = inspect.signature(self.func)
        try:
            type_hints = get_type_hints(self.func)
        except Exception:
            type_hints = {}

        properties = {}
        required = []

        for param_name, param in sig.parameters.items():
            param_type = type_hints.get(param_name, param.annotation if param.annotation != inspect.Parameter.empty else str)

            if self._is_injected_param(param_name, param_type):
                continue

            schema_type = self._python_type_to_json_type(param_type)
            schema_type["description"] = f"Parameter: {param_name}"
            properties[param_name] = schema_type

            if param.default == inspect.Parameter.empty:
                required.append(param_name)

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required
                }
            }
        }

    async def execute(self, arguments: Dict[str, Any], context: Optional[ToolContext] = None) -> Any:
        sig = inspect.signature(self.func)
        try:
            type_hints = get_type_hints(self.func)
        except Exception:
            type_hints = {}

        call_args = {}
        context = context or ToolContext()

        for param_name, param in sig.parameters.items():
            param_type = type_hints.get(param_name, param.annotation)

            if self._is_injected_param(param_name, param_type):
                if param_type is ToolContext or param_name in {"context", "ctx", "_context"}:
                    call_args[param_name] = context
                elif param_type in (discord.Message,) or param_name == "message":
                    call_args[param_name] = context.message
                elif param_type in (discord.abc.Messageable, discord.TextChannel) or param_name == "channel":
                    call_args[param_name] = context.channel
                elif param_type in (discord.User, discord.Member, discord.abc.User) or param_name == "author":
                    call_args[param_name] = context.author
                elif param_type in (commands.Bot,) or param_name == "bot":
                    call_args[param_name] = context.bot
                else:
                    call_args[param_name] = context
            elif param_name in arguments:
                call_args[param_name] = arguments[param_name]
            elif param.default != inspect.Parameter.empty:
                call_args[param_name] = param.default
            else:
                call_args[param_name] = None

        if self.is_async:
            return await self.func(**call_args)
        return self.func(**call_args)


def tool(name: Optional[str] = None, description: Optional[str] = None):
    """
    Decorator to mark a function as an AI callable tool.

    Usage:
        @tool(description="Calculates a mathematical expression")
        def calculate(expression: str) -> str:
            ...
    """
    def decorator(fn: Callable) -> Callable:
        fn._is_ai_tool = True
        fn._tool_name = name
        fn._tool_description = description
        return fn

    if callable(name):
        fn = name
        name = None
        return decorator(fn)
    return decorator
