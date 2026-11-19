# Security policy

## Reporting a vulnerability

Please report security problems **privately**, through GitHub's private
vulnerability reporting: open this repository's **Security** tab and choose
**Report a vulnerability**. Do not open a public issue or pull request for a
suspected vulnerability.

Useful in a report: what you found, the endpoint or file involved, the steps or
the request that shows it, and what an attacker could gain. Please do not send
real patient data or any other personal data; this project uses synthetic and
public data only, so a reproduction never needs it.

## Scope

The guarded API and its MCP server (`app.py`, `mcp_server.py`), the SQL guard and
its PHI rules (`sql_guard.py`, `phi_guard.py`, `phi_columns.yaml`), and the CI
workflows. The demo deploy runs on a free plan and may be asleep; please do not
load-test it.
