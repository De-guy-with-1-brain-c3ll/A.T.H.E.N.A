"""ATHENA Mission Control Dashboard - Textual TUI with integrated terminals and JARVIS ball."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.widgets import (
    Header, Footer, Static, Label, Button, Input, Log, TabbedContent, TabPane,
    DataTable, RichLog, Tab
)
from textual.widget import Widget
from textual.reactive import reactive
from textual.timer import Timer
from textual.geometry import Size, Offset
from textual.events import Mount, Resize
from textual.message import Message
from rich.text import Text
from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import Segment
from rich.style import Style

# =============================================================================
# JARVIS Ball Widget - Animated orb that reacts to ATHENA's state
# =============================================================================

class JarvisBall(Widget):
    """Animated JARVIS-style ball that pulses when ATHENA is talking, idle animates otherwise."""

    # Reactive state
    state: reactive[str] = reactive("idle")  # idle, listening, thinking, speaking
    pulse_phase: reactive[float] = reactive(0.0)
    rotation: reactive[float] = reactive(0.0)

    # Colors for different states
    STATE_COLORS = {
        "idle": ("#00d4ff", "#0066cc"),      # Cyan blue
        "listening": ("#00ff88", "#008844"), # Green
        "thinking": ("#ffaa00", "#cc6600"),  # Amber/Orange
        "speaking": ("#ff3366", "#cc0033"),  # Red/Pink
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._animation_timer: Optional[Timer] = None
        self._size = 20  # Ball diameter in characters

    def on_mount(self) -> None:
        """Start animation timer when mounted."""
        self._animation_timer = self.set_interval(1/30, self._animate)  # 30 FPS

    def on_unmount(self) -> None:
        """Stop animation timer when unmounted."""
        if self._animation_timer:
            self._animation_timer.stop()

    def _animate(self) -> None:
        """Animation frame callback."""
        self.pulse_phase += 0.1
        self.rotation += 0.05
        self.refresh()

    def set_state(self, state: str) -> None:
        """Update ball state."""
        if state in self.STATE_COLORS:
            self.state = state

    def render(self) -> RenderResult:
        """Render the animated ball."""
        primary, secondary = self.STATE_COLORS.get(self.state, self.STATE_COLORS["idle"])

        # Calculate pulse based on state
        if self.state == "speaking":
            # Fast pulse when speaking
            pulse = 0.8 + 0.2 * abs(__import__('math').sin(self.pulse_phase * 3))
        elif self.state == "thinking":
            # Medium pulse when thinking
            pulse = 0.85 + 0.15 * abs(__import__('math').sin(self.pulse_phase * 1.5))
        elif self.state == "listening":
            # Gentle pulse when listening
            pulse = 0.9 + 0.1 * abs(__import__('math').sin(self.pulse_phase))
        else:
            # Slow breathing idle animation
            pulse = 0.95 + 0.05 * abs(__import__('math').sin(self.pulse_phase * 0.5))

        # Create the ball rendering
        console = Console()
        width = self._size
        height = self._size // 2

        segments = []
        for y in range(height):
            line_segments = []
            for x in range(width):
                # Calculate distance from center
                cx, cy = width / 2, height / 2
                dx = (x - cx) / (width / 2)
                dy = (y - cy) / (height / 2)
                dist = (dx*dx + dy*dy) ** 0.5

                if dist <= pulse:
                    # Inside the ball - calculate intensity
                    intensity = 1.0 - (dist / pulse)

                    # Add rotation effect
                    angle = __import__('math').atan2(dy, dx) + self.rotation
                    swirl = (__import__('math').sin(angle * 3 + self.pulse_phase) + 1) / 2
                    intensity = intensity * 0.7 + swirl * 0.3 * intensity

                    # Choose color based on intensity
                    if intensity > 0.7:
                        color = primary
                    elif intensity > 0.4:
                        # Blend primary and secondary
                        color = secondary
                    else:
                        color = secondary

                    char = "●" if intensity > 0.5 else "○"
                    line_segments.append(Segment(char, Style(color=color)))
                else:
                    line_segments.append(Segment(" ", Style()))

            segments.extend(line_segments)
            segments.append(Segment("\n"))

        return segments


# =============================================================================
# Terminal Widget - Wrapper for subprocess terminals
# =============================================================================

class TerminalWidget(Widget):
    """A terminal widget that runs a shell process."""

    BINDINGS = [
        Binding("ctrl+c", "send_ctrl_c", "Send Ctrl+C"),
        Binding("ctrl+l", "clear", "Clear"),
    ]

    def __init__(
        self,
        shell: str,
        args: list[str],
        title: str,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.shell = shell
        self.args = args
        self.title = title
        self.cwd = cwd or str(Path.home() / "Desktop")
        self.env = env or os.environ.copy()
        self.process: Optional[subprocess.Popen] = None
        self.output_log = RichLog(highlight=True, markup=True, wrap=True)
        self._reader_task: Optional[asyncio.Task] = None
        self._running = False

    def compose(self) -> ComposeResult:
        yield Container(
            Label(self.title, classes="terminal-title"),
            self.output_log,
            classes="terminal-container"
        )

    def on_mount(self) -> None:
        """Start the terminal process when mounted."""
        self._start_terminal()

    def on_unmount(self) -> None:
        """Clean up process when unmounted."""
        self._stop_terminal()

    def _start_terminal(self) -> None:
        """Start the terminal subprocess."""
        try:
            # Prepare environment
            term_env = self.env.copy()
            term_env.update({
                "TERM": "xterm-256color",
                "COLORTERM": "truecolor",
            })

            # Build command
            if sys.platform == "win32":
                # On Windows, use the shell directly
                cmd = [self.shell] + self.args
                creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                cmd = [self.shell] + self.args
                creation_flags = 0

            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=self.cwd,
                env=term_env,
                text=True,
                bufsize=1,
                creationflags=creation_flags,
            )
            self._running = True

            # Start reading output
            self._reader_task = asyncio.create_task(self._read_output())

            self.output_log.write(f"[dim]Started {self.title} in {self.cwd}[/dim]")
            self.output_log.write(f"[dim]Command: {' '.join(cmd)}[/dim]")
            self.output_log.write("─" * 50)

        except Exception as e:
            self.output_log.write(f"[red]Failed to start {self.title}: {e}[/red]")

    def _stop_terminal(self) -> None:
        """Stop the terminal subprocess."""
        self._running = False
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None

        if self.process:
            try:
                if sys.platform == "win32":
                    self.process.terminate()
                else:
                    self.process.send_signal(subprocess.signal.SIGTERM)
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
            except Exception:
                pass
            self.process = None

    async def _read_output(self) -> None:
        """Read output from the terminal process."""
        while self._running and self.process and self.process.stdout:
            try:
                line = await asyncio.get_event_loop().run_in_executor(
                    None, self.process.stdout.readline
                )
                if not line:
                    break
                self.output_log.write(line.rstrip())
            except Exception:
                break

        if self._running:
            self.output_log.write("[dim]Terminal closed[/dim]")
            self._running = False

    def action_send_ctrl_c(self) -> None:
        """Send Ctrl+C to the terminal."""
        if self.process and self.process.stdin:
            try:
                if sys.platform == "win32":
                    self.process.send_signal(subprocess.signal.CTRL_C_EVENT)
                else:
                    self.process.send_signal(subprocess.signal.SIGINT)
            except Exception:
                pass

    def action_clear(self) -> None:
        """Clear the terminal output."""
        self.output_log.clear()


# =============================================================================
# Terminal Manager - Handles multiple terminal widgets
# =============================================================================

class TerminalManager(Widget):
    """Manages multiple terminal tabs."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.terminals: dict[str, TerminalWidget] = {}

    def compose(self) -> ComposeResult:
        with TabbedContent(id="terminal-tabs"):
            # PowerShell Terminal
            yield TabPane(
                "PowerShell",
                TerminalWidget(
                    shell="powershell.exe" if sys.platform == "win32" else "pwsh",
                    args=["-NoLogo", "-NoProfile", "-NoExit"],
                    title="PowerShell",
                    id="term-powershell"
                ),
                id="tab-powershell"
            )

            # CMD Terminal
            yield TabPane(
                "CMD",
                TerminalWidget(
                    shell="cmd.exe" if sys.platform == "win32" else "bash",
                    args=["/k"] if sys.platform == "win32" else ["-l"],
                    title="Command Prompt",
                    id="term-cmd"
                ),
                id="tab-cmd"
            )

            # Claude Code Terminal
            yield TabPane(
                "Claude Code",
                TerminalWidget(
                    shell=self._find_claude(),
                    args=[],
                    title="Claude Code",
                    id="term-claude"
                ),
                id="tab-claude"
            )

            # Codex Terminal
            yield TabPane(
                "Codex",
                TerminalWidget(
                    shell=self._find_codex(),
                    args=[],
                    title="OpenAI Codex",
                    id="term-codex"
                ),
                id="tab-codex"
            )

    def _find_claude(self) -> str:
        """Find the Claude Code binary."""
        # Try common locations
        candidates = [
            "claude",
            "claude-code",
            shutil.which("claude"),
            shutil.which("claude-code"),
        ]
        for c in candidates:
            if c and shutil.which(c):
                return c
        return "claude"  # Default fallback

    def _find_codex(self) -> str:
        """Find the Codex binary."""
        candidates = [
            "codex",
            shutil.which("codex"),
        ]
        for c in candidates:
            if c and shutil.which(c):
                return c
        return "codex"  # Default fallback


# =============================================================================
# Status Panel - Shows ATHENA status and metrics
# =============================================================================

class StatusPanel(Widget):
    """Status panel showing ATHENA connection and system info."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.connection_status = "Disconnected"
        self.voice_status = "Idle"
        self.latency = 0
        self._update_timer: Optional[Timer] = None

    def compose(self) -> ComposeResult:
        yield Container(
            Label("ATHENA Status", classes="panel-title"),
            Static("", id="connection-status"),
            Static("", id="voice-status"),
            Static("", id="latency-status"),
            classes="status-panel"
        )

    def on_mount(self) -> None:
        self._update_timer = self.set_interval(2, self._update_status)

    def on_unmount(self) -> None:
        if self._update_timer:
            self._update_timer.stop()

    def _update_status(self) -> None:
        """Update status display."""
        self.query_one("#connection-status", Static).update(
            f"[bold]Connection:[/bold] {self.connection_status}"
        )
        self.query_one("#voice-status", Static).update(
            f"[bold]Voice:[/bold] {self.voice_status}"
        )
        self.query_one("#latency-status", Static).update(
            f"[bold]Latency:[/bold] {self.latency}ms"
        )

    def update_connection(self, status: str) -> None:
        self.connection_status = status

    def update_voice(self, status: str) -> None:
        self.voice_status = status

    def update_latency(self, latency: int) -> None:
        self.latency = latency


# =============================================================================
# Main Dashboard App
# =============================================================================

class AthenaDashboard(App):
    """ATHENA Mission Control Dashboard - Textual TUI."""

    CSS = """
    /* =========================================================================
       Layout & Containers
       ========================================================================= */

    Screen {
        layout: horizontal;
        background: #0a0f1a;
    }

    #left-panel {
        width: 35%;
        min-width: 40;
        height: 100%;
        background: #0d1421;
        border-right: solid #1a2a3a;
        layout: vertical;
    }

    #right-panel {
        width: 65%;
        height: 100%;
        background: #0a0f1a;
        layout: vertical;
    }

    #top-section {
        height: 30%;
        min-height: 15;
        border-bottom: solid #1a2a3a;
    }

    #bottom-section {
        height: 70%;
        layout: vertical;
    }

    /* =========================================================================
       JARVIS Ball
       ========================================================================= */

    #jarvis-container {
        height: 100%;
        align: center middle;
        padding: 2;
    }

    JarvisBall {
        width: 30;
        height: 15;
        content-align: center middle;
    }

    #state-indicator {
        margin-top: 1;
        text-align: center;
        text-style: bold;
        color: #00d4ff;
    }

    /* =========================================================================
       Status Panel
       ========================================================================= */

    .status-panel {
        padding: 1 2;
        background: #0d1421;
        border-bottom: solid #1a2a3a;
    }

    .panel-title {
        color: #00d4ff;
        text-style: bold;
        margin-bottom: 1;
    }

    .status-panel Static {
        margin: 0 0 0 2;
        color: #88b0d0;
    }

    /* =========================================================================
       Terminals
       ========================================================================= */

    #terminal-tabs {
        height: 100%;
        background: #0a0f1a;
    }

    TabbedContent {
        height: 100%;
    }

    TabPane {
        height: 100%;
        background: #0a0f1a;
    }

    .terminal-container {
        height: 100%;
        layout: vertical;
        background: #080c14;
    }

    .terminal-title {
        height: 1;
        background: #1a2a3a;
        color: #00d4ff;
        text-style: bold;
        padding: 0 1;
        border-bottom: solid #2a3a4a;
    }

    RichLog {
        height: 1fr;
        background: #080c14;
        color: #c0d0e0;
        border: none;
        padding: 1;
        overflow-y: auto;
    }

    /* =========================================================================
       Tab Styling
       ========================================================================= */

    TabbedContent .tab {
        background: #1a2a3a;
        color: #88b0d0;
        padding: 0 2;
        margin: 0 1;
        border-top-left-radius: 1;
        border-top-right-radius: 1;
        text-style: bold;
    }

    TabbedContent .tab:hover {
        background: #2a3a4a;
        color: #00d4ff;
    }

    TabbedContent .tab.-active {
        background: #0a0f1a;
        color: #00d4ff;
        border-bottom: solid #00d4ff;
    }

    /* =========================================================================
       Header & Footer
       ========================================================================= */

    Header {
        background: #0d1421;
        color: #00d4ff;
        text-style: bold;
        border-bottom: solid #1a2a3a;
    }

    Footer {
        background: #0d1421;
        color: #88b0d0;
        border-top: solid #1a2a3a;
    }

    /* =========================================================================
       Input / Command Bar
       ========================================================================= */

    #command-bar {
        height: 3;
        background: #0d1421;
        border-top: solid #1a2a3a;
        layout: horizontal;
        padding: 0 1;
    }

    #command-input {
        width: 1fr;
        background: #1a2a3a;
        color: #c0d0e0;
        border: solid #2a3a4a;
        padding: 0 1;
    }

    #send-btn {
        width: 10;
        background: #00d4ff;
        color: #0a0f1a;
        text-style: bold;
        margin-left: 1;
    }

    #send-btn:hover {
        background: #00ffaa;
    }

    /* =========================================================================
       Scrollbar
       ========================================================================= */

    ScrollableContainer > .scrollbar {
        background: #0d1421;
        color: #2a3a4a;
    }

    ScrollableContainer > .scrollbar:hover {
        color: #00d4ff;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+t", "toggle_theme", "Theme"),
        Binding("f1", "help", "Help"),
        Binding("ctrl+1", "focus_terminal('powershell')", "PowerShell"),
        Binding("ctrl+2", "focus_terminal('cmd')", "CMD"),
        Binding("ctrl+3", "focus_terminal('claude')", "Claude"),
        Binding("ctrl+4", "focus_terminal('codex')", "Codex"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.jarvis_ball: Optional[JarvisBall] = None
        self.terminal_manager: Optional[TerminalManager] = None
        self.status_panel: Optional[StatusPanel] = None
        self._demo_timer: Optional[Timer] = None
        self._demo_state = 0

    def compose(self) -> ComposeResult:
        """Create the dashboard layout."""
        yield Header(show_clock=True, name="ATHENA Mission Control")

        with Horizontal():
            # Left Panel - JARVIS Ball + Status
            with Vertical(id="left-panel"):
                with Container(id="top-section", id="jarvis-container"):
                    self.jarvis_ball = JarvisBall(id="jarvis-ball")
                    yield self.jarvis_ball
                    yield Label("IDLE", id="state-indicator")

                self.status_panel = StatusPanel(id="status-panel")
                yield self.status_panel

            # Right Panel - Terminals
            with Vertical(id="right-panel"):
                with Container(id="bottom-section"):
                    self.terminal_manager = TerminalManager(id="terminal-manager")
                    yield self.terminal_manager

                # Command input bar at bottom
                with Horizontal(id="command-bar"):
                    yield Input(placeholder="Send command to active terminal...", id="command-input")
                    yield Button("Send", id="send-btn", variant="primary")

        yield Footer()

    def on_mount(self) -> None:
        """Initialize on mount."""
        # Start demo animation for JARVIS ball
        self._demo_timer = self.set_interval(3, self._demo_cycle)

        # Try to connect to ATHENA (if running)
        self._check_athena_connection()

    def on_unmount(self) -> None:
        if self._demo_timer:
            self._demo_timer.stop()

    def _demo_cycle(self) -> None:
        """Cycle through demo states for the JARVIS ball."""
        if not self.jarvis_ball:
            return

        states = ["idle", "listening", "thinking", "speaking"]
        self._demo_state = (self._demo_state + 1) % len(states)
        state = states[self._demo_state]

        self.jarvis_ball.set_state(state)
        self.query_one("#state-indicator", Label).update(state.upper())

        if self.status_panel:
            if state == "speaking":
                self.status_panel.update_voice("Speaking...")
            elif state == "listening":
                self.status_panel.update_voice("Listening...")
            elif state == "thinking":
                self.status_panel.update_voice("Processing...")
            else:
                self.status_panel.update_voice("Idle")

    def _check_athena_connection(self) -> None:
        """Check if ATHENA is running and update status."""
        # This would connect to the actual ATHENA service
        # For now, simulate connection
        if self.status_panel:
            self.status_panel.update_connection("Connected (Local)")
            self.status_panel.update_latency(12)

    def action_focus_terminal(self, terminal: str) -> None:
        """Switch to a specific terminal tab."""
        tab_map = {
            "powershell": "tab-powershell",
            "cmd": "tab-cmd",
            "claude": "tab-claude",
            "codex": "tab-codex",
        }
        if terminal in tab_map:
            tabs = self.query_one("#terminal-tabs", TabbedContent)
            tabs.active = tab_map[terminal]

    def action_toggle_theme(self) -> None:
        """Toggle between dark/light theme."""
        self.dark = not self.dark

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        if event.button.id == "send-btn":
            self._send_command()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key in command input."""
        self._send_command()

    def _send_command(self) -> None:
        """Send command to active terminal."""
        input_widget = self.query_one("#command-input", Input)
        command = input_widget.value.strip()
        if not command:
            return

        input_widget.value = ""

        # Find active terminal and send command
        tabs = self.query_one("#terminal-tabs", TabbedContent)
        active_tab = tabs.active

        # Map tab to terminal widget
        terminal_map = {
            "tab-powershell": "term-powershell",
            "tab-cmd": "term-cmd",
            "tab-claude": "term-claude",
            "tab-codex": "term-codex",
        }

        if active_tab in terminal_map:
            term_id = terminal_map[active_tab]
            terminal = self.query_one(f"#{term_id}", TerminalWidget)
            if terminal.process and terminal.process.stdin:
                try:
                    terminal.process.stdin.write(command + "\n")
                    terminal.process.stdin.flush()
                except Exception as e:
                    terminal.output_log.write(f"[red]Failed to send command: {e}[/red]")


# =============================================================================
# Entry Point
# =============================================================================

def main():
    """Run the ATHENA Mission Control Dashboard."""
    app = AthenaDashboard()
    app.run()


if __name__ == "__main__":
    main()