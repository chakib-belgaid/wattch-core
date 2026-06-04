use anyhow::Result;
use clap::{Parser, Subcommand};
use wattch_protocol::PROTOCOL_VERSION;

#[derive(Debug, Parser)]
#[command(name = "wattch")]
#[command(about = "Protocol-first energy and telemetry capture CLI")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Print protocol/client version.
    Version,

    /// List sources from a daemon. Socket transport is the next slice.
    Sources {
        #[arg(long, default_value = "/tmp/wattch-testd.sock")]
        socket: String,
    },

    /// Capture raw samples from a daemon. Artifact writing is the next slice.
    Capture {
        #[arg(long, default_value = "/tmp/wattch-testd.sock")]
        socket: String,

        #[arg(long)]
        out: String,

        #[arg(long, default_value_t = 5_000)]
        duration_ms: u64,
    },

    /// Validate a manifest or scenario file. Implementation follows the core validation model.
    Validate {
        #[arg(long)]
        input: String,
    },
}

fn main() -> Result<()> {
    let cli = Cli::parse();

    match cli.command {
        Command::Version => {
            println!("wattch protocol {}", PROTOCOL_VERSION);
        }
        Command::Sources { socket } => {
            println!("sources: socket transport not implemented yet ({socket})");
        }
        Command::Capture {
            socket,
            out,
            duration_ms,
        } => {
            println!(
                "capture: socket transport not implemented yet (socket={socket}, out={out}, duration_ms={duration_ms})"
            );
        }
        Command::Validate { input } => {
            println!("validate: scenario/manifest validation command pending ({input})");
        }
    }

    Ok(())
}
