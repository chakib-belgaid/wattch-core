use crate::service::{ServiceError, WattchService};
use serde_json::{json, Map, Value};
use std::io::{self, BufRead, Write};
use std::sync::Arc;

pub const CURRENT_MCP_VERSION: &str = "2025-11-25";
pub const SUPPORTED_MCP_VERSIONS: &[&str] = &["2025-11-25", "2025-06-18"];

const MAX_JSONRPC_LINE_BYTES: usize = 1024 * 1024;
const MAX_STRUCTURED_OUTPUT_BYTES: usize = 1024 * 1024;

pub struct McpServer {
    service: Arc<WattchService>,
    initialized: bool,
    client_ready: bool,
    protocol_version: String,
}

impl McpServer {
    pub fn new(service: Arc<WattchService>) -> Self {
        Self {
            service,
            initialized: false,
            client_ready: false,
            protocol_version: CURRENT_MCP_VERSION.to_string(),
        }
    }

    pub fn run(&mut self, reader: &mut impl BufRead, writer: &mut impl Write) -> io::Result<()> {
        loop {
            let line = match read_bounded_line(reader, MAX_JSONRPC_LINE_BYTES)? {
                BoundedLine::Eof => break,
                BoundedLine::TooLong => {
                    write_json_line(
                        writer,
                        &jsonrpc_error(
                            Value::Null,
                            -32700,
                            "JSON-RPC message exceeds the 1 MiB stdio limit",
                            None,
                        ),
                    )?;
                    continue;
                }
                BoundedLine::Line(line) => line,
            };
            if line.iter().all(u8::is_ascii_whitespace) {
                continue;
            }
            let message: Value = match serde_json::from_slice(&line) {
                Ok(message) => message,
                Err(error) => {
                    write_json_line(
                        writer,
                        &jsonrpc_error(
                            Value::Null,
                            -32700,
                            "invalid JSON-RPC message",
                            Some(json!({"detail": error.to_string()})),
                        ),
                    )?;
                    continue;
                }
            };
            if let Some(response) = self.handle_value(message) {
                write_json_line(writer, &response)?;
            }
        }
        self.service.shutdown_all();
        Ok(())
    }

    pub fn handle_value(&mut self, message: Value) -> Option<Value> {
        let object = match message.as_object() {
            Some(object) => object,
            None => {
                return Some(jsonrpc_error(
                    Value::Null,
                    -32600,
                    "JSON-RPC message must be an object",
                    None,
                ))
            }
        };
        if object.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
            return Some(jsonrpc_error(
                request_id(object),
                -32600,
                "jsonrpc must be \"2.0\"",
                None,
            ));
        }
        let method = match object.get("method").and_then(Value::as_str) {
            Some(method) => method,
            None => {
                return Some(jsonrpc_error(
                    request_id(object),
                    -32600,
                    "request method must be a string",
                    None,
                ))
            }
        };
        let id = object.get("id").cloned();
        let params = object.get("params").cloned().unwrap_or_else(|| json!({}));

        if id.is_none() {
            self.handle_notification(method, params);
            return None;
        }
        let id = id.unwrap_or(Value::Null);
        match method {
            "initialize" => Some(self.initialize(id, params)),
            "ping" => Some(jsonrpc_success(id, json!({}))),
            "tools/list" => {
                if !self.initialized {
                    return Some(not_initialized(id));
                }
                if let Some(cursor) = params.get("cursor") {
                    if !cursor.is_null() {
                        return Some(jsonrpc_error(
                            id,
                            -32602,
                            "wattch-mcp tools/list does not use pagination cursors",
                            None,
                        ));
                    }
                }
                Some(jsonrpc_success(id, json!({"tools": tool_definitions()})))
            }
            "tools/call" => {
                if !self.initialized {
                    return Some(not_initialized(id));
                }
                Some(self.call_tool(id, params))
            }
            _ => Some(jsonrpc_error(id, -32601, "method not found", None)),
        }
    }

    fn initialize(&mut self, id: Value, params: Value) -> Value {
        if self.initialized {
            return jsonrpc_error(id, -32600, "server is already initialized", None);
        }
        let requested = match params.get("protocolVersion").and_then(Value::as_str) {
            Some(version) => version,
            None => {
                return jsonrpc_error(
                    id,
                    -32602,
                    "initialize.params.protocolVersion must be a string",
                    None,
                )
            }
        };
        self.protocol_version = if SUPPORTED_MCP_VERSIONS.contains(&requested) {
            requested.to_string()
        } else {
            CURRENT_MCP_VERSION.to_string()
        };
        self.initialized = true;
        jsonrpc_success(
            id,
            json!({
                "protocolVersion": self.protocol_version,
                "capabilities": {
                    "tools": {"listChanged": false}
                },
                "serverInfo": {
                    "name": "wattch-mcp",
                    "title": "Wattch raw telemetry",
                    "version": env!("CARGO_PKG_VERSION")
                },
                "instructions": concat!(
                    "Use wattch_capture_start before an external workload and wattch_capture_stop after it. ",
                    "The server records capture-only WATTCH_WINDOW_START/END markers and raw evidence. ",
                    "It does not compute joules, aggregate sources, attribute process energy, compare models, ",
                    "or make optimization claims. Deterministic/testd telemetry validates plumbing only."
                )
            }),
        )
    }

    fn handle_notification(&mut self, method: &str, _params: Value) {
        if method == "notifications/initialized" && self.initialized {
            self.client_ready = true;
        }
        // Cancellation and unknown notifications are intentionally ignored.
        // Capture workers are controlled through explicit capture ids and are
        // stopped on stdio EOF, so a dropped client cannot orphan a daemon run.
    }

    fn call_tool(&self, id: Value, params: Value) -> Value {
        let name = match params.get("name").and_then(Value::as_str) {
            Some(name) => name,
            None => {
                return jsonrpc_error(id, -32602, "tools/call params.name must be a string", None)
            }
        };
        if !matches!(
            name,
            "wattch_sources"
                | "wattch_capture_start"
                | "wattch_capture_stop"
                | "wattch_inspect"
                | "wattch_validate"
        ) {
            return jsonrpc_error(
                id,
                -32602,
                "unknown Wattch tool",
                Some(json!({"tool": name})),
            );
        }
        let arguments = params
            .get("arguments")
            .cloned()
            .unwrap_or_else(|| json!({}));
        if !arguments.is_object() {
            return jsonrpc_error(
                id,
                -32602,
                "tools/call params.arguments must be an object",
                None,
            );
        }

        let result = match name {
            "wattch_sources" => self.service.sources(arguments),
            "wattch_capture_start" => self.service.capture_start(arguments),
            "wattch_capture_stop" => self.service.capture_stop(arguments),
            "wattch_inspect" => self.service.inspect(arguments),
            "wattch_validate" => self.service.validate(arguments),
            _ => unreachable!("tool name was checked above"),
        };
        match result {
            Ok(structured) => {
                if encoded_len(&structured) > MAX_STRUCTURED_OUTPUT_BYTES {
                    return jsonrpc_success(
                        id,
                        tool_error(ServiceError::new(
                            "OUTPUT_TOO_LARGE",
                            "structured result exceeds the 1 MiB MCP output limit",
                        )),
                    );
                }
                jsonrpc_success(id, tool_success(name, structured))
            }
            Err(error) => jsonrpc_success(id, tool_error(error)),
        }
    }
}

fn tool_success(name: &str, structured: Value) -> Value {
    let text = match name {
        "wattch_sources" => format!(
            "Discovered {} validated raw telemetry sources.",
            structured
                .get("source_count")
                .and_then(Value::as_u64)
                .unwrap_or(0)
        ),
        "wattch_capture_start" => format!(
            "Capture {} is ready; run the workload, then stop it with this capture_id.",
            structured
                .get("capture_id")
                .and_then(Value::as_str)
                .unwrap_or("<unknown>")
        ),
        "wattch_capture_stop" => format!(
            "Capture {} stopped and its raw artifact was synchronized.",
            structured
                .get("capture_id")
                .and_then(Value::as_str)
                .unwrap_or("<unknown>")
        ),
        "wattch_inspect" => "Artifact replay and integrity validation succeeded.".to_string(),
        "wattch_validate" => "Validation succeeded.".to_string(),
        _ => "Wattch tool completed.".to_string(),
    };
    json!({
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        "isError": false,
    })
}

fn tool_error(error: ServiceError) -> Value {
    let mut structured = json!({
        "ok": false,
        "error": {
            "code": error.code,
            "message": error.message,
        }
    });
    if let Some(data) = error.data {
        structured["error"]["data"] = data;
    }
    json!({
        "content": [{
            "type": "text",
            "text": format!(
                "{}: {}",
                structured["error"]["code"].as_str().unwrap_or("WATTCH_ERROR"),
                structured["error"]["message"].as_str().unwrap_or("tool failed")
            )
        }],
        "structuredContent": structured,
        "isError": true,
    })
}

fn tool_definitions() -> Vec<Value> {
    let descriptor_input = json!({
        "type": "object",
        "description": "A Wattch SourceDescriptor serialized with the implemented Rust JSON contract"
    });
    vec![
        json!({
            "name": "wattch_sources",
            "title": "Discover Wattch sources",
            "description": "Connect to the configured Wattch daemon and return every validated raw source descriptor. Synthetic/testd sources are identified as plumbing-only; no values are aggregated.",
            "inputSchema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {},
                "additionalProperties": false
            },
            "outputSchema": {"type": "object"},
            "annotations": {
                "readOnlyHint": true,
                "destructiveHint": false,
                "idempotentHint": true,
                "openWorldHint": false
            }
        }),
        json!({
            "name": "wattch_capture_start",
            "title": "Start raw Wattch capture",
            "description": "Start one background raw capture. Returns only after a first sample and daemon-stamped WATTCH_WINDOW_START marker are persisted. This does not execute a command; run the workload with the agent's normal shell, then call wattch_capture_stop.",
            "inputSchema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "minLength": 1, "maxLength": 256, "description": "Portable run directory name; generated when omitted"},
                    "out": {"type": "string", "minLength": 1, "description": "Output root inside the configured workspace/runs roots; defaults to --runs-dir"},
                    "interval_ms": {"type": "integer", "minimum": 10, "maximum": 60000, "default": 100},
                    "source_ids": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 4_294_967_295_u64}, "uniqueItems": true, "maxItems": 4096, "default": []},
                    "labels": {"type": "object", "maxProperties": 48, "additionalProperties": {"type": "string", "maxLength": 1024}, "default": {}}
                },
                "additionalProperties": false
            },
            "outputSchema": {"type": "object"},
            "annotations": {
                "readOnlyHint": false,
                "destructiveHint": false,
                "idempotentHint": false,
                "openWorldHint": false
            }
        }),
        json!({
            "name": "wattch_capture_stop",
            "title": "Stop raw Wattch capture",
            "description": "Record daemon-stamped WATTCH_WINDOW_END, retain a bounded post-END raw tail for interval evidence, stop/drain the matching capture, and synchronize its immutable artifact.",
            "inputSchema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "capture_id": {"type": "string", "minLength": 1, "description": "Opaque id returned by wattch_capture_start"}
                },
                "required": ["capture_id"],
                "additionalProperties": false
            },
            "outputSchema": {"type": "object"},
            "annotations": {
                "readOnlyHint": false,
                "destructiveHint": false,
                "idempotentHint": false,
                "openWorldHint": false
            }
        }),
        json!({
            "name": "wattch_inspect",
            "title": "Inspect Wattch artifact",
            "description": "Replay and integrity-check one raw run artifact, returning descriptor-bound counts, sequence gaps, event counts, and warnings. It never computes joules or an optimization comparison.",
            "inputSchema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "run": {"type": "string", "minLength": 1, "description": "Run directory inside the configured workspace/runs roots"}
                },
                "required": ["run"],
                "additionalProperties": false
            },
            "outputSchema": {"type": "object"},
            "annotations": {
                "readOnlyHint": true,
                "destructiveHint": false,
                "idempotentHint": true,
                "openWorldHint": false
            }
        }),
        json!({
            "name": "wattch_validate",
            "title": "Validate Wattch evidence",
            "description": "Validate exactly one inline source descriptor array, descriptor JSON file, or complete run artifact. File access is confined to configured roots.",
            "inputSchema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "sources": {"type": "array", "items": descriptor_input, "maxItems": 4096},
                    "input_path": {"type": "string", "minLength": 1},
                    "run": {"type": "string", "minLength": 1}
                },
                "oneOf": [
                    {"required": ["sources"]},
                    {"required": ["input_path"]},
                    {"required": ["run"]}
                ],
                "additionalProperties": false
            },
            "outputSchema": {"type": "object"},
            "annotations": {
                "readOnlyHint": true,
                "destructiveHint": false,
                "idempotentHint": true,
                "openWorldHint": false
            }
        }),
    ]
}

fn request_id(object: &Map<String, Value>) -> Value {
    object.get("id").cloned().unwrap_or(Value::Null)
}

fn not_initialized(id: Value) -> Value {
    jsonrpc_error(id, -32002, "server is not initialized", None)
}

fn jsonrpc_success(id: Value, result: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "result": result})
}

fn jsonrpc_error(id: Value, code: i64, message: &str, data: Option<Value>) -> Value {
    let mut error = json!({"code": code, "message": message});
    if let Some(data) = data {
        error["data"] = data;
    }
    json!({"jsonrpc": "2.0", "id": id, "error": error})
}

fn encoded_len(value: &Value) -> usize {
    serde_json::to_vec(value)
        .map(|bytes| bytes.len())
        .unwrap_or(usize::MAX)
}

fn write_json_line(writer: &mut impl Write, value: &Value) -> io::Result<()> {
    serde_json::to_writer(&mut *writer, value)?;
    writer.write_all(b"\n")?;
    writer.flush()
}

enum BoundedLine {
    Eof,
    Line(Vec<u8>),
    TooLong,
}

fn read_bounded_line(reader: &mut impl BufRead, limit: usize) -> io::Result<BoundedLine> {
    let mut output = Vec::new();
    let mut too_long = false;
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            if output.is_empty() && !too_long {
                return Ok(BoundedLine::Eof);
            }
            return Ok(if too_long {
                BoundedLine::TooLong
            } else {
                BoundedLine::Line(output)
            });
        }
        let newline = available.iter().position(|byte| *byte == b'\n');
        let consumed = newline.map_or(available.len(), |index| index + 1);
        if !too_long {
            let data_len = newline.unwrap_or(available.len());
            if output.len().saturating_add(data_len) > limit {
                too_long = true;
                output.clear();
            } else {
                output.extend_from_slice(&available[..data_len]);
            }
        }
        reader.consume(consumed);
        if newline.is_some() {
            return Ok(if too_long {
                BoundedLine::TooLong
            } else {
                BoundedLine::Line(output)
            });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::service::ServiceConfig;
    use std::io::{BufReader, Cursor};
    use std::path::Path;
    use std::time::Duration;

    fn server(root: &Path) -> McpServer {
        std::fs::create_dir_all(root).unwrap();
        let service = WattchService::new(ServiceConfig {
            default_socket: Some(root.join("missing.sock")),
            runs_dir: root.join("runs"),
            workspace_root: root.to_path_buf(),
            max_active_captures: 1,
            max_capture_duration: Duration::from_secs(5),
            startup_timeout: Duration::from_secs(1),
        })
        .unwrap();
        McpServer::new(Arc::new(service))
    }

    #[test]
    fn negotiates_only_the_explicit_structured_output_revisions() {
        assert_eq!(SUPPORTED_MCP_VERSIONS, ["2025-11-25", "2025-06-18"]);
        for version in SUPPORTED_MCP_VERSIONS {
            let root = std::env::temp_dir().join(format!(
                "wattch-mcp-init-{}-{}",
                std::process::id(),
                version.replace('-', "")
            ));
            let mut server = server(&root);
            let response = server
                .handle_value(json!({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
                }))
                .unwrap();
            assert_eq!(response["result"]["protocolVersion"], *version);
            let _ = std::fs::remove_dir_all(root);
        }

        let root = std::env::temp_dir().join(format!("wattch-mcp-old-init-{}", std::process::id()));
        let mut server = server(&root);
        let response = server
            .handle_value(json!({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"}
            }))
            .unwrap();
        assert_eq!(response["result"]["protocolVersion"], CURRENT_MCP_VERSION);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn lists_exact_bounded_tool_surface() {
        let root = std::env::temp_dir().join(format!("wattch-mcp-tools-{}", std::process::id()));
        let mut server = server(&root);
        server.handle_value(json!({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": CURRENT_MCP_VERSION}
        }));
        let response = server
            .handle_value(json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}))
            .unwrap();
        let names = response["result"]["tools"]
            .as_array()
            .unwrap()
            .iter()
            .map(|tool| tool["name"].as_str().unwrap())
            .collect::<Vec<_>>();
        assert_eq!(
            names,
            vec![
                "wattch_sources",
                "wattch_capture_start",
                "wattch_capture_stop",
                "wattch_inspect",
                "wattch_validate"
            ]
        );
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn stdio_is_newline_delimited_jsonrpc_and_tool_failures_are_results() {
        let root = std::env::temp_dir().join(format!("wattch-mcp-stdio-{}", std::process::id()));
        let mut server = server(&root);
        let input = format!(
            "{}\n{}\n{}\n",
            json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":CURRENT_MCP_VERSION}}),
            json!({"jsonrpc":"2.0","method":"notifications/initialized"}),
            json!({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"wattch_sources","arguments":{}}})
        );
        let mut reader = BufReader::new(Cursor::new(input));
        let mut output = Vec::new();
        server.run(&mut reader, &mut output).unwrap();
        let lines = String::from_utf8(output).unwrap();
        let messages = lines
            .lines()
            .map(|line| serde_json::from_str::<Value>(line).unwrap())
            .collect::<Vec<_>>();
        assert_eq!(messages.len(), 2);
        assert_eq!(messages[1]["result"]["isError"], true);
        assert!(messages[1]["result"]["structuredContent"].is_object());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn oversized_line_is_drained_without_desynchronizing_next_request() {
        let mut bytes = vec![b'x'; MAX_JSONRPC_LINE_BYTES + 1];
        bytes.push(b'\n');
        bytes.extend_from_slice(b"{}\n");
        let mut reader = BufReader::new(Cursor::new(bytes));
        assert!(matches!(
            read_bounded_line(&mut reader, MAX_JSONRPC_LINE_BYTES).unwrap(),
            BoundedLine::TooLong
        ));
        assert!(matches!(
            read_bounded_line(&mut reader, MAX_JSONRPC_LINE_BYTES).unwrap(),
            BoundedLine::Line(line) if line == b"{}"
        ));
    }
}
