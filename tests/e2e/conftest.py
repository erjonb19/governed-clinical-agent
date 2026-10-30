"""End-to-end tests: real processes, real sockets, browsers.

Collected only when RUN_E2E=1 (the e2e workflows set it), so the default
`pytest` run -- tests.yml and its pass count -- never starts servers or
browsers. A marker in addopts would not do: tests.yml passes its own `-m`,
which replaces addopts' one.
"""

import os

collect_ignore_glob = [] if os.environ.get("RUN_E2E") == "1" else ["test_*.py"]
