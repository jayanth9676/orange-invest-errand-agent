import sys
import importlib.util
from pathlib import Path

# Add errand-agent and root to sys.path
root_dir = Path(__file__).resolve().parent.parent
agent_dir = Path(__file__).resolve().parent
if str(agent_dir) not in sys.path:
    sys.path.insert(0, str(agent_dir))
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

root_check_web_app = root_dir / "check_web_app.py"
spec = importlib.util.spec_from_file_location("root_check_web_app", root_check_web_app)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

if __name__ == "__main__":
    sys.exit(mod.main())
