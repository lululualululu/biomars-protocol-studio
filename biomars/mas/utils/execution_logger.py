import logging
import json
import os
import time
from datetime import datetime
from typing import Any, Dict, Optional

try:
    import psutil  # type: ignore
except Exception:
    psutil = None

class ExecutionLogger:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(ExecutionLogger, cls).__new__(cls)
        return cls._instance

    def __init__(self, log_dir: str = "var/logs"):
        if hasattr(self, "initialized"):
            return
            
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = os.path.join(log_dir, f"execution_{timestamp}.jsonl")
        
        # Setup audit logger
        self.audit_logger = logging.getLogger("audit_logger")
        self.audit_logger.setLevel(logging.INFO)
        # Prevent adding multiple handlers if re-initialized
        if not self.audit_logger.handlers:
            audit_handler = logging.FileHandler(os.path.join(log_dir, f"audit_{timestamp}.log"))
            formatter = logging.Formatter('%(asctime)s - %(message)s')
            audit_handler.setFormatter(formatter)
            self.audit_logger.addHandler(audit_handler)
            
        self.initialized = True

    def _get_system_metrics(self) -> Dict[str, float]:
        try:
            if psutil is None:
                return {}
            process = psutil.Process(os.getpid())
            return {
                "cpu_percent": process.cpu_percent(),
                "memory_mb": process.memory_info().rss / 1024 / 1024
            }
        except Exception:
            return {}

    def log_event(self, 
                  event_type: str, 
                  component: str, 
                  phase: str, 
                  data: Dict[str, Any], 
                  level: str = "INFO"):
        
        entry = {
            "timestamp": datetime.now().isoformat(),
            "timestamp_ms": time.time(),
            "event_type": event_type,
            "component": component,
            "phase": phase,
            "system_metrics": self._get_system_metrics(),
            "data": data
        }
        
        # Write to structured log
        try:
            with open(self.log_file, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            print(f"Failed to write log: {e}")
            
        # Write to audit log if critical or specific events
        if level in ["WARNING", "ERROR", "CRITICAL"] or event_type in ["STATE_CHANGE", "CRITICAL_OP"]:
            self.audit_logger.info(f"[{level}] {component} - {event_type}: {json.dumps(data, default=str)}")

    def log_agent_interaction_markdown(self, step_name: str, agent_name: str, inputs: Dict[str, Any], outputs: Dict[str, Any]):
        """
        Log agent interaction in a beautiful markdown format.
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        md_file = os.path.join(self.log_dir, f"workflow_trace_{datetime.now().strftime('%Y%m%d')}.md")
        
        # Helper to format JSON for markdown
        def format_json(d):
            return json.dumps(d, indent=2, default=str)
            
        md_content = f"""
## 🕒 {timestamp} - Step: {step_name}

**🤖 Agent:** `{agent_name}`

<details open>
<summary><b>📥 Input Context</b></summary>

```json
{format_json(inputs)}
```
</details>

<details open>
<summary><b>📤 Output Result</b></summary>

```json
{format_json(outputs)}
```
</details>

---
"""
        try:
            with open(md_file, "a") as f:
                f.write(md_content)
        except Exception as e:
            print(f"Failed to write markdown log: {e}")

# Global instance
execution_logger = ExecutionLogger()
