import os
import sys
from pathlib import Path

# Set paths and working directory to errand-agent
root_dir = Path(__file__).resolve().parent
errand_agent_dir = root_dir / "errand-agent"
if errand_agent_dir.is_dir():
    if str(errand_agent_dir) not in sys.path:
        sys.path.insert(0, str(errand_agent_dir))
    try:
        os.chdir(str(errand_agent_dir))
    except Exception:
        pass

# Execute the main streamlit_app
app_file = errand_agent_dir / "streamlit_app.py"
with open(app_file, "r", encoding="utf-8") as f:
    code = f.read()

globs = dict(globals())
globs["__file__"] = str(app_file)
exec(code, globs)
