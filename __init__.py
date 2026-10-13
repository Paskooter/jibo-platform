"""Native Jibo platform plugin for Hermes Agent.

Native mode connects to the custom Be bridge over one WebSocket per robot.
The archived ROM adapter remains available with ``extra.mode=rom`` as an
explicit compatibility fallback.
"""

from __future__ import annotations


def check_requirements() -> bool:
    """Return whether the host-side transport dependencies are available."""
    try:
        import httpx  # noqa: F401
        import websockets  # noqa: F401
        import yaml  # noqa: F401
        import fastapi  # noqa: F401
        from PIL import Image  # noqa: F401
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config) -> bool:
    """Validate native multi-device or explicitly selected ROM settings."""
    from .native_adapter import native_validate_config

    return native_validate_config(config)


def is_connected(config) -> bool:
    """Report configured status; the live adapter owns transport health."""
    from .native_adapter import native_is_connected

    return native_is_connected(config)


def register(ctx) -> None:
    """Register the Jibo platform without touching Hermes core."""
    from .adapter import JiboRomAdapter
    from .native_adapter import NativeJiboAdapter, native_mode
    from .catalog_tool import (
        JIBO_FIND_EXPRESSION_SCHEMA,
        JIBO_PLAY_EXPRESSION_SCHEMA,
        jibo_find_expression_available,
        jibo_find_expression_tool,
        jibo_play_expression_available,
        jibo_play_expression_tool,
    )
    from .image_tool import (
        JIBO_DISPLAY_IMAGE_SCHEMA,
        JIBO_SHOW_EYE_SCHEMA,
        jibo_display_image_available,
        jibo_display_image_tool,
        jibo_show_eye_available,
        jibo_show_eye_tool,
    )
    from .status import (
        on_interim_message,
        on_post_llm_call,
        on_post_tool_call,
        on_pre_llm_call,
        on_pre_tool_call,
        on_session_end,
    )
    from .tool import JIBO_SPEAK_SCHEMA, jibo_speak_available, jibo_speak_tool
    from .native_tools import (
        JIBO_CAMERA_SCHEMA,
        JIBO_OBSERVE_SCHEMA,
        JIBO_CAPABILITIES_SCHEMA,
        JIBO_CHAT_LOG_SCHEMA,
        JIBO_COLOR_BAR_SCHEMA,
        JIBO_EXPRESS_SCHEMA,
        JIBO_LAUNCH_SKILL_SCHEMA,
        JIBO_LOOK_SCHEMA,
        JIBO_LISTEN_SCHEMA,
        JIBO_OPEN_WEB_PANEL_SCHEMA,
        JIBO_CLOSE_WEB_PANEL_SCHEMA,
        JIBO_SKILLS_SCHEMA,
        JIBO_STATUS_SCHEMA,
        JIBO_STOP_SCHEMA,
        JIBO_STOP_SKILL_SCHEMA,
        jibo_camera_available,
        jibo_camera_tool,
        jibo_observe_available,
        jibo_observe_tool,
        jibo_capabilities_available,
        jibo_capabilities_tool,
        jibo_chat_log_available,
        jibo_chat_log_tool,
        jibo_color_bar_available,
        jibo_color_bar_tool,
        jibo_express_available,
        jibo_express_tool,
        jibo_launch_skill_available,
        jibo_launch_skill_tool,
        jibo_listen_available,
        jibo_listen_tool,
        jibo_look_available,
        jibo_look_tool,
        jibo_open_web_panel_available,
        jibo_open_web_panel_tool,
        jibo_close_web_panel_available,
        jibo_close_web_panel_tool,
        jibo_skills_available,
        jibo_skills_tool,
        jibo_status_available,
        jibo_status_tool,
        jibo_stop_available,
        jibo_stop_tool,
        jibo_stop_skill_available,
        jibo_stop_skill_tool,
    )

    from pathlib import Path

    ctx.register_skill(
        name="jibo-hermes-integration",
        path=Path(__file__).parent / "agent-skills/jibo-hermes-integration/SKILL.md",
        description="Use the existing Jibo platform safely with the owner's Hermes gateway.",
    )
    ctx.register_tool(
        name="jibo_speak",
        toolset="jibo",
        schema=JIBO_SPEAK_SCHEMA,
        handler=jibo_speak_tool,
        check_fn=jibo_speak_available,
        description=JIBO_SPEAK_SCHEMA["description"],
        emoji="🤖",
    )
    ctx.register_tool(
        name="jibo_display_image",
        toolset="jibo",
        schema=JIBO_DISPLAY_IMAGE_SCHEMA,
        handler=jibo_display_image_tool,
        check_fn=jibo_display_image_available,
        description=JIBO_DISPLAY_IMAGE_SCHEMA["description"],
        emoji="🖼️",
    )
    ctx.register_tool(
        name="jibo_show_eye",
        toolset="jibo",
        schema=JIBO_SHOW_EYE_SCHEMA,
        handler=jibo_show_eye_tool,
        check_fn=jibo_show_eye_available,
        description=JIBO_SHOW_EYE_SCHEMA["description"],
        emoji="👁️",
    )
    ctx.register_tool(
        name="jibo_find_expression",
        toolset="jibo",
        schema=JIBO_FIND_EXPRESSION_SCHEMA,
        handler=jibo_find_expression_tool,
        check_fn=jibo_find_expression_available,
        description=JIBO_FIND_EXPRESSION_SCHEMA["description"],
        emoji="✨",
    )
    ctx.register_tool(
        name="jibo_play_expression",
        toolset="jibo",
        schema=JIBO_PLAY_EXPRESSION_SCHEMA,
        handler=jibo_play_expression_tool,
        check_fn=jibo_play_expression_available,
        description=JIBO_PLAY_EXPRESSION_SCHEMA["description"],
        emoji="💃",
    )
    native_tools = (
        ("jibo_status", JIBO_STATUS_SCHEMA, jibo_status_tool, jibo_status_available, "📊"),
        ("jibo_list_capabilities", JIBO_CAPABILITIES_SCHEMA, jibo_capabilities_tool, jibo_capabilities_available, "🧭"),
        ("jibo_chat_log", JIBO_CHAT_LOG_SCHEMA, jibo_chat_log_tool, jibo_chat_log_available, "💬"),
        ("jibo_stop", JIBO_STOP_SCHEMA, jibo_stop_tool, jibo_stop_available, "🛑"),
        ("jibo_listen", JIBO_LISTEN_SCHEMA, jibo_listen_tool, jibo_listen_available, "👂"),
        ("jibo_color_bar", JIBO_COLOR_BAR_SCHEMA, jibo_color_bar_tool, jibo_color_bar_available, "🌈"),
        ("jibo_express", JIBO_EXPRESS_SCHEMA, jibo_express_tool, jibo_express_available, "🙆"),
        ("jibo_look", JIBO_LOOK_SCHEMA, jibo_look_tool, jibo_look_available, "👀"),
        ("jibo_list_skills", JIBO_SKILLS_SCHEMA, jibo_skills_tool, jibo_skills_available, "🧩"),
        ("jibo_launch_skill", JIBO_LAUNCH_SKILL_SCHEMA, jibo_launch_skill_tool, jibo_launch_skill_available, "▶️"),
        ("jibo_stop_skill", JIBO_STOP_SKILL_SCHEMA, jibo_stop_skill_tool, jibo_stop_skill_available, "⏹️"),
        ("jibo_open_web_panel", JIBO_OPEN_WEB_PANEL_SCHEMA, jibo_open_web_panel_tool, jibo_open_web_panel_available, "🌐"),
        ("jibo_close_web_panel", JIBO_CLOSE_WEB_PANEL_SCHEMA, jibo_close_web_panel_tool, jibo_close_web_panel_available, "⏹️"),
        ("jibo_camera_capture", JIBO_CAMERA_SCHEMA, jibo_camera_tool, jibo_camera_available, "📷"),
        ("jibo_observe", JIBO_OBSERVE_SCHEMA, jibo_observe_tool, jibo_observe_available, "👁️"),
    )
    for name, schema, handler, check_fn, emoji in native_tools:
        ctx.register_tool(
            name=name,
            toolset="jibo",
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            description=schema["description"],
            emoji=emoji,
        )
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("on_interim_message", on_interim_message)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("on_session_end", on_session_end)

    def adapter_factory(cfg):
        if native_mode(getattr(cfg, "extra", {}) or {}) == "rom":
            return JiboRomAdapter(cfg)
        return NativeJiboAdapter(cfg)

    ctx.register_platform(
        name="jibo",
        label="Jibo Robot",
        adapter_factory=adapter_factory,
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=[],
        install_hint=(
            "Install the Python dependencies declared in jibo-platform/plugin.yaml "
            "in the selected Hermes environment; see its README for consent and compatibility."
        ),
        max_message_length=3000,
        emoji="🤖",
        allow_update_command=False,
        platform_hint=(
            "You are speaking through a Jibo robot over its native Be bridge. Keep replies short and natural "
            "(normally one or two sentences), informal, and conversational. Your normal "
            "assistant reply is automatically streamed, spoken, and shown in Jibo's chat; "
            "answer directly without a speech tool call. Use jibo_speak only for an intentional "
            "spoken progress update or a reply needing an explicit style or safe ESML. "
            "With that tool, final=true means the complete answer has been delivered; "
            "finish with the same short plain-text answer for the saved transcript. "
            "The adapter suppresses duplicate delivery. Do not speak raw tool output, markdown, URLs, "
            "JSON, symbols, private chain-of-thought, memory/self-improvement "
            "notes, or implementation commentary. Jibo's persistent on-screen chat "
            "shows the user's utterance immediately, streams the reply, and shows a "
            "small live state; it must remain the only text surface for this conversation. "
            "Do not use jibo_display_text or try to replace the chat with a face text view. "
            "Never put chain-of-thought or raw tool output on screen. A normal answer "
            "ends the turn. Ask a follow-up question only when you need the user's next reply. "
            "The native adapter opens a bounded follow-up window for a final question when "
            "configured; use jibo_listen only for an explicitly requested listening window. "
            "Do not call jibo_listen after a completed answer just to be polite. If the user asks for "
            "a camera view or an Internet image, obtain a real direct HTTP(S) "
            "image URL with the appropriate tool, use jibo_display_image before "
            "describing it. The image stays in the conversation for the user to inspect. "
            "Do not invent image URLs or display a search-results page."
            " Curated native expressions use jibo_express and the connected robot's "
            "advertised capabilities/vocabulary; never guess expression keys. "
            "Raw jibo_find_expression/jibo_play_expression tools require an optional, "
            "owner-authorized local AnimDB index that is not shipped with this plugin. "
            "Do not promise raw dances, music, camera or Web Panel availability without "
            "checking the connected robot's grants and capabilities. "
            "The explicit extra.mode=rom setting remains available "
            "only as a legacy compatibility fallback."
        ),
    )
