"""cleat - a headless terminal layer for AI agents.

A persistent PTY shell session whose byte stream is parsed for OSC 133 marks and
exposed to an agent over MCP as structured results (stdout, exit code, files
touched), plus a virtual screen for interactive programs and TUIs.
"""

__version__ = "0.1.0"
