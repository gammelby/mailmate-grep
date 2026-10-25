"""PreToolUse hook: block Bash commands that use a heredoc and exceed 512 bytes.

Long heredocs hang the Bash tool on the maintainer's machine. Exit code 2
blocks the call and shows stderr to the agent.
"""
import json
import re
import sys

LIMIT = 512

payload = json.load(sys.stdin)
command = payload.get("tool_input", {}).get("command", "")
if re.search(r"<<-?\s*['\"]?\w", command) and len(command.encode("utf-8")) > LIMIT:
    sys.stderr.write(
        "Blocked: Bash command with a heredoc is over 512 bytes "
        "(long heredocs hang this shell). Write the content to a file "
        "with the Write tool and pass the file path instead "
        "(for git commits: git commit -F <file>).\n"
    )
    sys.exit(2)
sys.exit(0)
