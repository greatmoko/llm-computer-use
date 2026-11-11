# LLM-Computer-Use

A portable Windows desktop automation skill for agents that can run local commands and read images. The calling agent decides what to do; the skill does not start a separate model or agent session.

## Requirements

- Windows, Python 3.10 or newer, and Pillow.
- An agent host that supports Skills, reading local images, and keeping a background process alive between commands.

## Install

Copy this folder into a Skills directory recognized by your agent, then install the Python dependency:

```powershell
python -m pip install -r requirements.txt
```

## Use

Ask the agent to use `llm-computer-use`, or read [SKILL.md](SKILL.md) and [the action reference](references/actions.md). The script starts by showing the desktop and returning an image of the primary monitor. It supports mouse and keyboard input, screenshots, the clipboard, and commands. During operation, a desktop indicator is visible; press Escape twice to stop.

Screenshots use JPEG. Full-screen images are resized and coordinate conversion is handled by the executor. Use a window or region screenshot when finer detail is needed.

## License

[MIT](LICENSE).
