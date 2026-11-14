from uuid import MAX
from discord import Attachment, HTTPException
from typing import List, Optional, Any, Dict
import aiohttp, base64, json, logging, mimetypes, re

from .modules.tool import ToolContext

logger = logging.getLogger(__name__)


class JsonUtils:
    @staticmethod
    def loadJson(filename: str, default_data: list | dict):
        try:
            with open(filename, "r", encoding="utf-8") as file:
                return json.load(file)
        except (FileNotFoundError, json.JSONDecodeError):
            with open(filename, "w", encoding="utf-8") as file:
                json.dump(default_data, file, indent=4, ensure_ascii=False)
            return default_data

    @staticmethod
    def saveJson(filename: str, data):
        with open(filename, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4, ensure_ascii=False)


class MemoryUtils:
    @staticmethod
    def updateMemory(chat_file, chat_memory, MAX_HISTORY, user_id, username, message, role):
        if user_id not in chat_memory:
            chat_memory[user_id] = {
                "username": username,
                "messages": []
            }

        if role == "user":
            prefix = f"{username}:"
            if message.strip().startswith(prefix):
                new_content = message
            else:
                new_content = f"{username}: {message}"
        else:
            new_content = message

        chat_memory[user_id]["messages"].append({"role": role, "content": new_content})

        if len(chat_memory[user_id]["messages"]) > MAX_HISTORY:
            chat_memory[user_id]["messages"].pop(0)

        JsonUtils.saveJson(chat_file, chat_memory)

    @staticmethod
    def buildMemoryMessage(base_text: str, image_descriptions: List[str]) -> str:
        parts = []
        if base_text and base_text.strip():
            parts.append(base_text.strip())
        for desc in image_descriptions:
            parts.append(f"[🖼️ {desc}]")
        if not parts:
            return "[🖼️ Người dùng đã gửi hình ảnh]"
        return "\n".join(parts)


class AttachmentUtils:
    @staticmethod
    def isImage(attachment: Attachment) -> bool:
        if attachment.content_type:
            return attachment.content_type.startswith("image/")
        mime, _ = mimetypes.guess_type(attachment.filename)
        return bool(mime and mime.startswith("image/"))

    @staticmethod
    def cacheImage(IMAGE_CACHE, MAX_IMAGE_CACHE_ITEMS, url: str, data_url: str) -> None:
        if url in IMAGE_CACHE:
            IMAGE_CACHE.move_to_end(url)
            IMAGE_CACHE[url] = data_url
            return

        IMAGE_CACHE[url] = data_url
        if len(IMAGE_CACHE) > MAX_IMAGE_CACHE_ITEMS:
            IMAGE_CACHE.popitem(last=False)

    @staticmethod
    async def attachmentToUrl(IMAGE_CACHE, MAX_IMAGE_CACHE_ITEMS, MAX_IMAGE_BYTES, attachment: Attachment) -> str:
        if attachment.url in IMAGE_CACHE:
            return IMAGE_CACHE[attachment.url]

        if attachment.size and attachment.size > MAX_IMAGE_BYTES:
            return ""

        try:
            image_bytes = await attachment.read()
        except HTTPException:
            return ""

        if len(image_bytes) > MAX_IMAGE_BYTES:
            return ""

        mime_type = attachment.content_type or mimetypes.guess_type(attachment.filename)[0] or "application/octet-stream"
        encoded = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:{mime_type};base64,{encoded}"
        AttachmentUtils.cacheImage(IMAGE_CACHE, MAX_IMAGE_CACHE_ITEMS, attachment.url, data_url)
        return data_url


class FormatUtils:
    _SUBSCRIPT_MAP = {
        '0': '₀', '1': '₁', '2': '₂', '3': '₃', '4': '₄',
        '5': '₅', '6': '₆', '7': '₇', '8': '₈', '9': '₉',
        '+': '₊', '-': '₋', '=': '₌', '(': '₍', ')': '₎',
        'a': 'ₐ', 'e': 'ₑ', 'h': 'ₕ', 'i': 'ᵢ', 'j': 'ⱼ',
        'k': 'ₖ', 'l': 'ₗ', 'm': 'ₘ', 'n': 'ₙ', 'o': 'ₒ',
        'p': 'ₚ', 'r': 'ᵣ', 's': 'ₛ', 't': 'ₜ', 'u': 'ᵤ',
        'v': 'ᵥ', 'x': 'ₓ'
    }

    _SUPERSCRIPT_MAP = {
        '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴',
        '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹',
        '+': '⁺', '-': '⁻', '=': '⁼', '(': '⁽', ')': '⁾',
        'n': 'ⁿ', 'i': 'ⁱ'
    }

    _LATEX_REPLACEMENTS = [
        (r"\\rightarrow|\\to|\\longrightarrow", "→"),
        (r"\\leftarrow|\\longleftarrow", "←"),
        (r"\\Rightarrow|\\implies", "⇒"),
        (r"\\Leftarrow", "⇐"),
        (r"\\Leftrightarrow|\\iff", "⇔"),
        (r"\\leftrightarrow", "↔"),
        (r"\\rightleftharpoons|\\leftrightharpoons", "⇌"),
        (r"\\pm", "±"),
        (r"\\mp", "∓"),
        (r"\\times", "×"),
        (r"\\cdot", "·"),
        (r"\\div", "÷"),
        (r"\\approx", "≈"),
        (r"\\neq", "≠"),
        (r"\\le|\\leq", "≤"),
        (r"\\ge|\\geq", "≥"),
        (r"\\infty", "∞"),
        (r"\\sqrt\{([^}]+)\}", r"√(\1)"),
        (r"\\sqrt", "√"),
        (r"\\frac\{([^}]+)\}\{([^}]+)\}", r"(\1)/(\2)"),
        (r"\\degree|\\circ", "°"),
        (r"\\Delta", "Δ"),
        (r"\\alpha", "α"),
        (r"\\beta", "β"),
        (r"\\gamma", "γ"),
        (r"\\pi", "π"),
        (r"\\theta", "θ"),
        (r"\\lambda", "λ"),
        (r"\\mu", "μ"),
        (r"\\sigma", "σ"),
        (r"\\omega", "ω"),
        (r"\\text\{([^}]+)\}", r"\1"),
        (r"\\mathrm\{([^}]+)\}", r"\1"),
        (r"\\mathbf\{([^}]+)\}", r"\1"),
        (r"\\ce\{([^}]+)\}", r"\1"),
    ]

    @classmethod
    def cleanLatexForDiscord(cls, text: str) -> str:
        """
        Converts LaTeX math and chemistry markup (e.g. $$2H_2 + O_2 \\rightarrow 2H_2O$$)
        into clean Discord-compatible Unicode representations (2H₂ + O₂ → 2H₂O).
        """
        if not text:
            return ""

        res = text
        for pat, repl in cls._LATEX_REPLACEMENTS:
            res = re.sub(pat, repl, res)

        def sub_repl(m):
            chars = m.group(1) or m.group(2)
            return "".join(cls._SUBSCRIPT_MAP.get(c, c) for c in chars)

        res = re.sub(r"_\{([0-9a-zA-Z+\-=()]+)\}|_([0-9a-zA-Z])", sub_repl, res)

        def sup_repl(m):
            chars = m.group(1) or m.group(2)
            return "".join(cls._SUPERSCRIPT_MAP.get(c, c) for c in chars)

        res = re.sub(r"\^\{([0-9+\-=()ni]+)\}|\^([0-9+\-ni])", sup_repl, res)

        # Strip $$...$$ and $...$ delimiters
        res = re.sub(r"\$\$(.*?)\$\$", r"\1", res, flags=re.DOTALL)
        res = re.sub(r"(?<!\$)\$(?!\$)(.*?)(?<!\$)\$(?!\$)", r"\1", res)

        return res


class ChatUtils:
    @staticmethod
    def sanitizeMessages(messages):
        """
        Hàm này đảm bảo:
        1. Không bao giờ có 2 role giống nhau đứng cạnh nhau (merge content lại).
        2. Message đầu tiên sau System PHẢI là User (nếu là Assistant thì xóa).
        """
        if not messages:
            return []

        sanitized = []
        
        # Tách system ra (nếu có)
        has_system = messages[0]["role"] == "system"
        start_index = 1 if has_system else 0
        
        if has_system:
            sanitized.append(messages[0])

        # Xử lý phần còn lại
        temp_messages = messages[start_index:]
        
        # Bỏ qua các tin nhắn Assistant ở đầu (nếu bị cắt mất User tương ứng)
        while temp_messages and temp_messages[0]["role"] == "assistant":
            temp_messages.pop(0)

        for msg in temp_messages:
            if not sanitized:
                sanitized.append(msg)
                continue

            last_msg = sanitized[-1]

            # Nếu role trùng nhau -> Merge content (ngoại trừ tool role hoặc tool_calls)
            if (
                last_msg.get("role") == msg.get("role")
                and msg.get("role") != "tool"
                and not last_msg.get("tool_calls")
                and not msg.get("tool_calls")
            ):
                old_content = last_msg.get("content", "") or ""
                new_content = msg.get("content", "") or ""

                # --- FIX: NẾU CÓ CHỨA ẢNH (LIST), PHẢI MERGE CHUẨN JSON VISION ---
                if isinstance(old_content, str) and isinstance(new_content, str):
                    # Nếu cả 2 đều là chữ thường, nối chuỗi bình thường
                    last_msg["content"] = f"{old_content}\n\n{new_content}"
                else:
                    # Ép tất cả về dạng List chuẩn OpenAI
                    old_list = [{"type": "text", "text": old_content}] if isinstance(old_content, str) else old_content
                    new_list = [{"type": "text", "text": new_content}] if isinstance(new_content, str) else new_content
            
                    # Cộng 2 mảng lại với nhau không làm mất cấu trúc Base64
                    last_msg["content"] = old_list + new_list
            else:
                sanitized.append(msg)
                
        return sanitized

    @staticmethod
    def isLikelyToolCallText(text: str) -> bool:
        """Checks if streaming text looks like a raw JSON tool call to prevent dumping it to Discord."""
        if not text:
            return False
        stripped = text.strip()
        if not stripped:
            return False

        if stripped.startswith("```"):
            first_nl = stripped.find("\n")
            if first_nl == -1:
                return stripped in ("```", "```json", "```JSON")
            inner = stripped[first_nl + 1:].strip()
            if not inner and stripped[:first_nl].lower() in ("```json", "```"):
                return True
            stripped = inner

        markers = (
            '{"name"',
            '{"name":',
            '{\n  "name":',
            '{"function"',
            '{"function":',
            '{"tool"',
            '{"tool":',
            '<tool_call>',
            '"name": "run_shell_command"',
            '"name": "calculate"',
            '"name": "roll_dice"',
            '"name": "fetch_url"',
            '"name": "get_current_time"',
            '"name": "toggle_process_console"',
            '"name": "send_terminal_keys"',
            '"name": "kill_running_command"'
        )
        for m in markers:
            if m in stripped:
                return True

        return False

    @staticmethod
    def extractTextToolCalls(text: str, available_tools: dict) -> list[dict]:
        """
        Detects and extracts tool calls emitted as text/markdown JSON by the LLM.
        Includes robust regex-based extraction for malformed JSON containing unescaped
        quotes or multi-line bash/python commands.
        """
        clean_text = text.strip()

        # Look for code block fence
        fence_m = re.search(r"```(?:json)?\s*(.*?)\s*```", clean_text, re.DOTALL)
        if fence_m:
            cand_str = fence_m.group(1).strip()
        else:
            xml_m = re.search(r"<tool_call>(.*?)</tool_call>", clean_text, re.DOTALL)
            if xml_m:
                cand_str = xml_m.group(1).strip()
            else:
                cand_str = clean_text

        # 1. Attempt standard json.loads
        start = cand_str.find("{")
        end = cand_str.rfind("}")
        candidate_json = cand_str[start:end+1] if (start != -1 and end != -1 and end > start) else cand_str

        try:
            parsed = json.loads(candidate_json, strict=False)
            if isinstance(parsed, dict):
                fn_name = parsed.get("name") or parsed.get("function") or parsed.get("tool")
                if fn_name and fn_name in available_tools:
                    args = parsed.get("arguments", parsed.get("parameters", {}))
                    if isinstance(args, str):
                        try:
                            args = json.loads(args, strict=False)
                        except Exception:
                            args = {"command": args}
                    elif not isinstance(args, dict):
                        args = {}
                    return [{"name": fn_name, "arguments": args, "raw": candidate_json}]
        except Exception:
            pass

        # 2. Robust Regex-based parser for malformed tool JSON emitted by LLMs
        name_m = re.search(r'["\'](?:name|function|tool)["\']\s*:\s*["\']([a-zA-Z0-9_\-]+)["\']', cand_str)
        if not name_m:
            return []

        fn_name = name_m.group(1)
        if fn_name not in available_tools:
            return []

        args = {}

        if fn_name == "run_shell_command":
            cmd_m = re.search(
                r'["\']command["\']\s*:\s*["\'](.*?)["\']\s*(?:,\s*["\'](?:timeout_seconds|timeout|show_console)["\']|\s*\}\s*\})',
                cand_str,
                re.DOTALL
            )
            if not cmd_m:
                cmd_m = re.search(r'["\']command["\']\s*:\s*["\'](.*)', cand_str, re.DOTALL)
                if cmd_m:
                    raw_c = cmd_m.group(1).rstrip()
                    raw_c = re.sub(r'["\']\s*(?:,\s*["\'][a-zA-Z0-9_]+["\']\s*:.*?)?\s*\}\s*\}?\s*$', '', raw_c, flags=re.DOTALL)
                    args["command"] = raw_c
            else:
                args["command"] = cmd_m.group(1)

            if "command" in args:
                args["command"] = args["command"].replace('\\"', '"').replace('\\n', '\n')

            to_m = re.search(r'["\'](?:timeout_seconds|timeout)["\']\s*:\s*([0-9]+)', cand_str)
            if to_m:
                args["timeout_seconds"] = int(to_m.group(1))

            sc_m = re.search(r'["\']show_console["\']\s*:\s*(true|false)', cand_str, re.IGNORECASE)
            if sc_m:
                args["show_console"] = sc_m.group(1).lower() == "true"

        elif fn_name in ("send_terminal_keys", "toggle_process_console", "kill_running_command"):
            for k in ["keys", "action"]:
                m = re.search(rf'["\']{k}["\']\s*:\s*["\'](.*?)["\']', cand_str, re.DOTALL)
                if m:
                    args[k] = m.group(1).replace('\\"', '"')
            for b in ["show", "hide", "enter", "raw"]:
                m = re.search(rf'["\']{b}["\']\s*:\s*(true|false)', cand_str, re.IGNORECASE)
                if b_val := m:
                    args[b] = b_val.group(1).lower() == "true"

        else:
            for str_key in ["expr", "expression", "url", "dice", "notation"]:
                m = re.search(rf'["\']{str_key}["\']\s*:\s*["\'](.*?)["\']\s*(?:,\s*["\']|\s*\}})', cand_str, re.DOTALL)
                if m:
                    args[str_key] = m.group(1).replace('\\"', '"')

            for int_key in ["sides", "count", "modifier", "timeout"]:
                m = re.search(rf'["\']{int_key}["\']\s*:\s*([0-9\-]+)', cand_str)
                if m:
                    args[int_key] = int(m.group(1))

        if args or fn_name in available_tools:
            return [{"name": fn_name, "arguments": args, "raw": cand_str}]

        return []

    @staticmethod
    async def responseStream(
        OPENAI_API_URL,
        OPENAI_API_KEY, 
        SYSTEM_PROMPT, 
        MODEL_NAME,
        chat_memory, 
        prompt, 
        author_name, 
        channel, 
        image_data_urls=None, 
        user_text_override=None,
        extension_manager=None,
        bot=None,
        max_tool_iterations=5
    ):
        if not OPENAI_API_KEY:
            await channel.send("❌ Error: OpenAI API key not set.")
            return ""

        system_prompt = SYSTEM_PROMPT
        if extension_manager:
            addons = extension_manager.get_system_prompt_addon()
            if addons:
                system_prompt = f"{system_prompt}\n\n# Active Extensions Context:\n{addons}".strip()

        image_data_urls = image_data_urls or []
        user_text = user_text_override if user_text_override is not None else prompt.content
        user_text = user_text if user_text is not None else ""

        conversation_history = chat_memory.get(str(prompt.author.id), {}).get("messages", [])

        messages = []
        if system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt})

        messages.extend({"role": msg["role"], "content": msg["content"]} for msg in conversation_history)

        user_payload = []
        trimmed_text = user_text.strip()
        if trimmed_text:
            user_payload.append({"type": "text", "text": f"{author_name}: {trimmed_text}"})
        elif image_data_urls:
            user_payload.append({"type": "text", "text": f"{author_name} đã gửi {len(image_data_urls)} hình ảnh."})

        if image_data_urls:
            for data_url in image_data_urls:
                if data_url:
                    user_payload.append({"type": "image_url", "image_url": {"url": data_url}})

        if not user_payload:
            user_payload.append({"type": "text", "text": f"{author_name} sent an empty message."})

        messages.append({"role": "user", "content": user_payload})
        final_messages = ChatUtils.sanitizeMessages(messages)

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}"
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            iteration = 0
            accumulated_assistant_response = ""

            while iteration < max_tool_iterations:
                iteration += 1

                payload = {
                    "model": MODEL_NAME,
                    "messages": final_messages,
                    "max_tokens": 4096,
                    "stream": True,
                    "user": str(prompt.author.id),
                    "user_display_name": f"{prompt.author.display_name} ({prompt.author.name})"
                }

                if extension_manager and getattr(extension_manager, "enabled", True) and extension_manager.get_tool_schemas():
                    payload["tools"] = extension_manager.get_tool_schemas()
                    payload["tool_choice"] = "auto"

                try:
                    async with session.post(OPENAI_API_URL, json=payload, timeout=aiohttp.ClientTimeout(300)) as resp:
                        if resp.status != 200:
                            error_text = await resp.text()
                            await channel.send(f"❌ Lỗi từ OpenAI API: {resp.status} - {error_text}")
                            return accumulated_assistant_response

                        full_response = ""
                        processed_idx = 0
                        send_idx = 0
                        codeblock_stack = []
                        tool_calls_buffer = {}

                        async def drain_lines_up_to(limit):
                            nonlocal send_idx, full_response
                            while True:
                                idx = full_response.find("\n", send_idx, limit)
                                if idx == -1:
                                    break
                                piece = full_response[send_idx: idx + 1]
                                cleaned_piece = FormatUtils.cleanLatexForDiscord(piece.rstrip("\n"))
                                if cleaned_piece.strip() != "":
                                    await channel.send(cleaned_piece)
                                send_idx = idx + 1

                        async for raw in resp.content:
                            chunk = raw.decode("utf-8")
                            for line in chunk.splitlines():
                                line = line.strip()
                                if not line or not line.startswith("data:"):
                                    continue
                                json_str = line[len("data:"):].strip()
                                if json_str == "[DONE]":
                                    break
                                try:
                                    j = json.loads(json_str)
                                except Exception as e:
                                    continue

                                delta_obj = j.get("choices", [{}])[0].get("delta", {})
                                if not delta_obj:
                                    continue

                                # Track tool calls stream fragments
                                if "tool_calls" in delta_obj and delta_obj["tool_calls"]:
                                    for tc_delta in delta_obj["tool_calls"]:
                                        idx = tc_delta.get("index", 0)
                                        if idx not in tool_calls_buffer:
                                            tool_calls_buffer[idx] = {"id": "", "name": "", "arguments": ""}
                                        if tc_delta.get("id"):
                                            tool_calls_buffer[idx]["id"] += tc_delta["id"]
                                        fn = tc_delta.get("function", {})
                                        if fn.get("name"):
                                            tool_calls_buffer[idx]["name"] += fn["name"]
                                        if fn.get("arguments"):
                                            tool_calls_buffer[idx]["arguments"] += fn["arguments"]

                                delta = delta_obj.get("content")
                                if isinstance(delta, list):
                                    delta = "".join(part.get("text", "") for part in delta if part.get("type") == "text")
                                elif not isinstance(delta, str):
                                    delta = ""

                                if not delta:
                                    continue

                                prev_len = len(full_response)
                                full_response += delta
                                new_len = len(full_response)

                                # Only stream text to Discord if tool call has not been triggered and does not look like a text tool call
                                if not tool_calls_buffer and not ChatUtils.isLikelyToolCallText(full_response):
                                    search_start = max(0, processed_idx - 2)
                                    new_section = full_response[search_start:]
                                    offset = search_start
                                    find_pos = new_section.find("```")
                                    while find_pos != -1:
                                        abs_pos = offset + find_pos
                                        if abs_pos >= processed_idx:
                                            if not codeblock_stack:
                                                codeblock_stack.append(abs_pos)
                                            else:
                                                start_pos = codeblock_stack.pop()
                                                end_pos = abs_pos + 3
                                                if send_idx < start_pos:
                                                    await drain_lines_up_to(start_pos)
                                                block = full_response[start_pos:end_pos]
                                                if block.strip() != "":
                                                    await channel.send(block)
                                                send_idx = end_pos
                                        find_pos = new_section.find("```", find_pos + 3)

                                    processed_idx = new_len
                                    if not codeblock_stack:
                                        await drain_lines_up_to(len(full_response))

                        # Check fallback text-based tool calls (for models/proxies that emit JSON in content)
                        fallback_calls = []
                        if not tool_calls_buffer and extension_manager:
                            fallback_calls = ChatUtils.extractTextToolCalls(full_response, extension_manager.tools)
                            if fallback_calls:
                                for idx, tc in enumerate(fallback_calls):
                                    tool_calls_buffer[idx] = {
                                        "id": f"call_text_{iteration}_{idx}",
                                        "name": tc["name"],
                                        "arguments": json.dumps(tc["arguments"])
                                    }

                        # If tool calls were generated, execute them and loop for follow-up response
                        if tool_calls_buffer and extension_manager:
                            tool_calls_list = []
                            for idx in sorted(tool_calls_buffer.keys()):
                                tc = tool_calls_buffer[idx]
                                tool_calls_list.append({
                                    "id": tc["id"] or f"call_{idx}_{iteration}",
                                    "type": "function",
                                    "function": {
                                        "name": tc["name"],
                                        "arguments": tc["arguments"]
                                    }
                                })

                            final_messages.append({
                                "role": "assistant",
                                "content": None if fallback_calls else (full_response if full_response.strip() else None),
                                "tool_calls": tool_calls_list
                            })

                            tool_context = ToolContext(
                                bot=bot,
                                channel=channel,
                                message=prompt,
                                author=prompt.author
                            )

                            for tc in tool_calls_list:
                                fn_name = tc["function"]["name"]  # ty: ignore[invalid-argument-type]
                                raw_args = tc["function"]["arguments"]  # ty: ignore[invalid-argument-type]
                                try:
                                    parsed_args = json.loads(raw_args, strict=False) if raw_args.strip() else {}
                                except Exception:
                                    parsed_args = {}

                                result = await extension_manager.execute_tool(fn_name, parsed_args, tool_context)
                                final_messages.append({
                                    "role": "tool",
                                    "tool_call_id": tc["id"],
                                    "name": fn_name,
                                    "content": result
                                })

                            # Continue to next iteration to stream assistant response with tool result
                            continue

                        # If no tool calls (or final turn), check if output was a failed text tool call
                        if not tool_calls_buffer and ChatUtils.isLikelyToolCallText(full_response):
                            # Prevent dumping raw malformed JSON tool calls to Discord chat
                            logger.warning(f"Suppressed malformed tool call from being sent to Discord: {full_response[:200]}")
                            final_messages.append({
                                "role": "assistant",
                                "content": full_response
                            })
                            final_messages.append({
                                "role": "user",
                                "content": "Error: Your tool call format was invalid JSON. Please call the tool using clean arguments without unescaped quotes or newlines."
                            })
                            continue

                        # If no tool calls (or final turn), flush remaining text buffer
                        if codeblock_stack:
                            if full_response[send_idx:].strip() != "":
                                await channel.send(full_response[send_idx:].strip())
                            send_idx = len(full_response)
                        else:
                            if send_idx < len(full_response):
                                tail = full_response[send_idx:].strip()
                                cleaned_tail = FormatUtils.cleanLatexForDiscord(tail)
                                if cleaned_tail.strip() != "":
                                    await channel.send(cleaned_tail)

                        accumulated_assistant_response = FormatUtils.cleanLatexForDiscord(full_response.strip())
                        return accumulated_assistant_response

                except aiohttp.ClientError as e:
                    await channel.send(f"❌ Lỗi kết nối đến OpenAI API: {e}")
                    return accumulated_assistant_response

            return accumulated_assistant_response


def triggerMatched(text: str, triggers: list[str]) -> bool:
    text = text.lower()
    for kw in triggers:
        pattern = r"\b" + re.escape(kw.lower()) + r"\b"
        if re.search(pattern, text):
            return True
    return False


__all__ = [
    "JsonUtils",
    "MemoryUtils",
    "ChatUtils",
    "AttachmentUtils",
    "FormatUtils",
    "triggerMatched"
]