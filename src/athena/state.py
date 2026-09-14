from enum import Enum


class AgentState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    USING_TOOL = "using_tool"
    SPEAKING = "speaking"
    CANCELLING = "cancelling"
    RECOVERING = "recovering"
