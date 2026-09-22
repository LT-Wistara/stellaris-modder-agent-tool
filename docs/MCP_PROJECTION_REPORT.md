# MCP compact projection (1.5.0)

> Historical 1.5.0 measurement: these byte counts predate the text-only MCP result format. Current responses serialize the same compact projection once in `content[0].text` and omit `structuredContent`.

Measured against saved 1.4.1 replies for identical tools/call requests. Bytes include the complete JSON-RPC response, short text content and newline (`json.dumps(..., ensure_ascii=False)`). Initialization and stderr are excluded.

| Request | Before bytes | After bytes | Reduction |
|---|---:|---:|---:|
| search fuzzy | 4056 | 605 | 85.1% |
| get overloads | 1724 | 650 | 62.3% |
| get modifier | 1608 | 545 | 66.1% |
| validate unknown | 1283 | 701 | 45.4% |
| validate known | 1091 | 224 | 79.5% |

Detailed Database/Validator results were compared with the saved baseline structured result and remained exactly equal in all five cases. CLI uses those detailed results. Only MCP projects them.

Get retains complete CWT text and derived modifier scopes. Search retains ranked candidates, evidence status and fuzzy scores; weak related rows and internal scoring explanations are omitted. Validate retains actionable/uncertain findings and status counts; routine confirmed findings and provenance are omitted. List/file responses and the 800-line limit are unchanged.

Exact requests and byte counts: [MCP_PROJECTION_REPORT.json](MCP_PROJECTION_REPORT.json).
