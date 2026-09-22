# Output size comparison

> Historical measurement: current MCP responses serialize the compact projection once in `content[0].text` and omit `structuredContent`; the table below records the earlier wire format.

Actual stdout UTF-8 bytes including trailing newline; CLI keeps indent=2. MCP measures each complete tools/call JSON-RPC response including envelope and newline, excluding initialize and stderr. Same arguments and Python before/after.

Validation case uses context="trigger". Both get calls omit type. No whitespace/minification changes were made.

| Query | CLI before | CLI after | Reduction | MCP before | MCP after | Reduction |
|---|---:|---:|---:|---:|---:|---:|
| `get has_background_job` | 8980 | 2066 | 76.99% | 15004 | 1724 | 88.51% |
| `get <district.capped>_max_add` | 8580 | 2058 | 76.01% | 14024 | 1608 | 88.53% |
| `validate has_magic_planet = yes` | 8058 | 1536 | 80.94% | 13408 | 1283 | 90.43% |

All sizes are UTF-8 bytes. For each case, after expanding definition_ref and excluding only the old upstream/new dataset envelope, the before/after payloads compare exactly equal. This includes status, all definitions and findings, sources, annotations, fields, modifier scopes, suggestions and explanatory messages.

At the time of this measurement MCP kept the full structured result once and a short text summary. Full manifest details remain available through stats/corpus and UPSTREAM.json.
