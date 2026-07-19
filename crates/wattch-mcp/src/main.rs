use clap::Parser;
use std::io::{self, BufReader};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use wattch_mcp::{McpServer, ServiceConfig, WattchService};

#[derive(Debug, Parser)]
#[command(name = "wattch-mcp")]
#[command(about = "MCP stdio adapter for Wattch raw telemetry and artifacts")]
struct Args {
    /// Explicit Wattch daemon socket. When omitted, trusted powerd then testd
    /// runtime paths are probed. /tmp sockets always require this option.
    #[arg(long)]
    socket: Option<PathBuf>,

    /// Default artifact output root.
    #[arg(long, default_value = "runs")]
    runs_dir: PathBuf,

    /// Root for relative paths and the file-access boundary of MCP tools.
    #[arg(long, default_value = ".")]
    workspace_root: PathBuf,

    /// Maximum simultaneous background captures owned by this MCP process.
    #[arg(long, default_value_t = 1)]
    max_active_captures: usize,

    /// Automatic safety deadline for a start/stop capture.
    #[arg(long, default_value_t = 3_600_000)]
    max_capture_ms: u64,

    /// Deadline for first sample plus WATTCH_WINDOW_START acknowledgement.
    #[arg(long, default_value_t = 10_000)]
    startup_timeout_ms: u64,
}

fn main() {
    if let Err(error) = run() {
        // stdout is exclusively MCP JSON-RPC. Diagnostics belong on stderr.
        eprintln!("wattch-mcp: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args = Args::parse();
    let service = Arc::new(WattchService::new(ServiceConfig {
        default_socket: args.socket,
        runs_dir: args.runs_dir,
        workspace_root: args.workspace_root,
        max_active_captures: args.max_active_captures,
        max_capture_duration: Duration::from_millis(args.max_capture_ms),
        startup_timeout: Duration::from_millis(args.startup_timeout_ms),
    })?);
    let mut server = McpServer::new(service);
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut reader = BufReader::new(stdin.lock());
    let mut writer = stdout.lock();
    server.run(&mut reader, &mut writer)?;
    Ok(())
}
