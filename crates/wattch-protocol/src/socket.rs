//! Shared default socket-path resolution for wattch daemons and clients.
//!
//! Fine-grained RAPL energy counters are a documented side-channel signal
//! (PLATYPUS, CVE-2020-8694); recent kernels restrict `energy_uj` to root.
//! `wattch-powerd` re-exposes those counters over a Unix socket, so the
//! socket's location and permissions are the actual security boundary. This
//! module centralizes where that socket lives so `wattch-powerd`,
//! `wattch-testd`, and `wattch-cli` agree without duplicating the logic.
//!
//! Resolution order for automatic client discovery (highest to lowest
//! privilege):
//! 1. `/run/wattch/<name>` — root-owned runtime directory (systemd
//!    `RuntimeDirectory=wattch`, mode 0750; socket mode 0660 after bind).
//! 2. `$XDG_RUNTIME_DIR/wattch/<name>` — user-private runtime directory.
//!
//! A non-root daemon may still *bind* a per-uid `/tmp` path when no private
//! runtime directory exists, but clients never auto-discover it. `/tmp` is
//! attacker-writable and existence checks do not authenticate a Unix socket;
//! development clients must opt in with an explicit `--socket` path.

use std::env;
use std::path::PathBuf;

use nix::unistd::Uid;

const RUN_WATTCH_DIR: &str = "/run/wattch";
const TMP_DIR: &str = "/tmp";
const XDG_RUNTIME_DIR_VAR: &str = "XDG_RUNTIME_DIR";

/// Where a daemon should bind a socket named `name` (e.g. `"wattch-powerd.sock"`).
pub fn default_bind_path(name: &str) -> PathBuf {
    bind_path_for(
        name,
        Uid::effective().as_raw(),
        xdg_runtime_dir().as_deref(),
    )
}

/// Candidate paths a client should probe, in priority order. The client
/// should connect to the first one that exists and error out listing all of
/// them if none do.
pub fn default_connect_candidates(name: &str) -> Vec<PathBuf> {
    connect_candidates_for(name, xdg_runtime_dir().as_deref())
}

fn xdg_runtime_dir() -> Option<String> {
    filter_xdg_runtime_dir(env::var(XDG_RUNTIME_DIR_VAR).ok())
}

/// Pure filtering logic behind `xdg_runtime_dir`, split out so the
/// empty-string-means-unset rule can be unit tested directly without
/// mutating the process environment (see module tests).
fn filter_xdg_runtime_dir(value: Option<String>) -> Option<String> {
    value.filter(|dir| !dir.is_empty())
}

/// Per-uid fallback filename, e.g. `wattch-powerd-501.sock` from
/// `wattch-powerd.sock` and uid 501.
fn per_uid_tmp_name(name: &str, uid: u32) -> String {
    match name.rsplit_once('.') {
        Some((stem, ext)) if !ext.is_empty() => format!("{stem}-{uid}.{ext}"),
        _ => format!("{name}-{uid}"),
    }
}

fn bind_path_for(name: &str, euid: u32, xdg: Option<&str>) -> PathBuf {
    if euid == 0 {
        return PathBuf::from(RUN_WATTCH_DIR).join(name);
    }
    if let Some(dir) = xdg {
        return PathBuf::from(dir).join("wattch").join(name);
    }
    PathBuf::from(TMP_DIR).join(per_uid_tmp_name(name, euid))
}

fn connect_candidates_for(name: &str, xdg: Option<&str>) -> Vec<PathBuf> {
    let mut candidates = vec![PathBuf::from(RUN_WATTCH_DIR).join(name)];
    if let Some(dir) = xdg {
        candidates.push(PathBuf::from(dir).join("wattch").join(name));
    }
    candidates
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bind_path_root_uses_run_wattch() {
        let path = bind_path_for("wattch-powerd.sock", 0, Some("/run/user/501"));
        assert_eq!(path, PathBuf::from("/run/wattch/wattch-powerd.sock"));
    }

    #[test]
    fn bind_path_root_ignores_xdg_even_when_set() {
        let path = bind_path_for("wattch-powerd.sock", 0, None);
        assert_eq!(path, PathBuf::from("/run/wattch/wattch-powerd.sock"));
    }

    #[test]
    fn bind_path_non_root_uses_xdg_when_set() {
        let path = bind_path_for("wattch-powerd.sock", 501, Some("/run/user/501"));
        assert_eq!(
            path,
            PathBuf::from("/run/user/501/wattch/wattch-powerd.sock")
        );
    }

    #[test]
    fn bind_path_non_root_falls_back_to_tmp_when_no_xdg() {
        let path = bind_path_for("wattch-powerd.sock", 501, None);
        assert_eq!(path, PathBuf::from("/tmp/wattch-powerd-501.sock"));
    }

    #[test]
    fn bind_path_treats_empty_xdg_as_unset() {
        // The public wrapper filters empty XDG_RUNTIME_DIR before calling
        // this; exercise the private fn directly with `None` to document the
        // contract (empty-string filtering lives in `xdg_runtime_dir`).
        let path = bind_path_for("wattch-powerd.sock", 501, None);
        assert_eq!(path, PathBuf::from("/tmp/wattch-powerd-501.sock"));
    }

    #[test]
    fn per_uid_tmp_name_inserts_uid_before_extension() {
        assert_eq!(
            per_uid_tmp_name("wattch-powerd.sock", 501),
            "wattch-powerd-501.sock"
        );
    }

    #[test]
    fn per_uid_tmp_name_handles_no_extension() {
        assert_eq!(per_uid_tmp_name("wattch-powerd", 501), "wattch-powerd-501");
    }

    #[test]
    fn connect_candidates_use_only_trusted_runtime_directories() {
        let candidates = connect_candidates_for("wattch-powerd.sock", Some("/run/user/501"));
        assert_eq!(
            candidates,
            vec![
                PathBuf::from("/run/wattch/wattch-powerd.sock"),
                PathBuf::from("/run/user/501/wattch/wattch-powerd.sock"),
            ]
        );
    }

    #[test]
    fn connect_candidates_without_xdg_skips_that_entry() {
        let candidates = connect_candidates_for("wattch-powerd.sock", None);
        assert_eq!(
            candidates,
            vec![PathBuf::from("/run/wattch/wattch-powerd.sock")]
        );
    }

    #[test]
    fn filter_xdg_runtime_dir_unset_stays_none() {
        assert_eq!(filter_xdg_runtime_dir(None), None);
    }

    #[test]
    fn filter_xdg_runtime_dir_treats_empty_as_unset() {
        assert_eq!(filter_xdg_runtime_dir(Some(String::new())), None);
    }

    #[test]
    fn filter_xdg_runtime_dir_passes_through_non_empty() {
        assert_eq!(
            filter_xdg_runtime_dir(Some("/run/user/501".to_string())),
            Some("/run/user/501".to_string())
        );
    }

    #[test]
    fn default_bind_path_matches_current_process_euid() {
        // Smoke test for the public entry point: it must agree with the
        // private fn given the real euid and real XDG_RUNTIME_DIR.
        let name = "wattch-smoke-test.sock";
        let expected = bind_path_for(
            name,
            Uid::effective().as_raw(),
            xdg_runtime_dir().as_deref(),
        );
        assert_eq!(default_bind_path(name), expected);
    }

    #[test]
    fn default_connect_candidates_matches_current_process_euid() {
        let name = "wattch-smoke-test.sock";
        let expected = connect_candidates_for(name, xdg_runtime_dir().as_deref());
        assert_eq!(default_connect_candidates(name), expected);
    }
}
