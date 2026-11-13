"""Compatibility alias for initial_plan; new code should use initial_plan.

Both names resolve to the same module so existing imports and patches still work.
"""

import sys
from . import initial_plan

sys.modules[__name__] = initial_plan
