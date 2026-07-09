//! Kernel-verified peer authentication for daemon sockets.
//!
//! RAPL-derived energy data is a documented side channel (PLATYPUS,
//! CVE-2020-8694), so file permissions on the socket are not the only
//! gate: the server also checks the connecting process's credentials as
//! reported by the kernel (`SO_PEERCRED`) at accept() time. Non-Linux
//! builds are development environments only (RAPL is Linux-only); they
//! have no credential facility wired up and allow all peers, with a
//! warning at startup.

use std::io;
use std::os::unix::net::UnixStream;

/// Kernel-reported credentials of a connected Unix-socket peer.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PeerCreds {
    pub uid: u32,
    pub gid: u32,
    pub pid: i32,
}

/// Connection-level access policy, checked once per accepted connection.
#[derive(Clone, Debug)]
pub struct PeerPolicy {
    pub allowed_uids: Vec<u32>,
    pub allowed_gids: Vec<u32>,
}

impl PeerPolicy {
    /// The default policy plus explicit grants: uid 0 and the daemon's own
    /// effective uid are always allowed. `extra_gids` match the peer's
    /// *effective* gid only — `SO_PEERCRED` does not report supplementary
    /// groups; those are the job of the socket file's 0660 permissions.
    pub fn with_grants(extra_uids: &[u32], extra_gids: &[u32]) -> Self {
        let mut allowed_uids = vec![0, nix::unistd::geteuid().as_raw()];
        allowed_uids.extend_from_slice(extra_uids);
        Self {
            allowed_uids,
            allowed_gids: extra_gids.to_vec(),
        }
    }

    pub fn permits(&self, uid: u32, gid: u32) -> bool {
        self.allowed_uids.contains(&uid) || self.allowed_gids.contains(&gid)
    }
}

/// Read the peer's kernel-verified credentials.
///
/// `Ok(None)` means the platform has no credential facility (non-Linux
/// dev builds); the caller treats that as allow-all. An `Err` means the
/// read failed on a platform that has one — callers must treat that as a
/// denial, never as an allow.
#[cfg(target_os = "linux")]
pub fn peer_credentials(stream: &UnixStream) -> io::Result<Option<PeerCreds>> {
    let creds = nix::sys::socket::getsockopt(stream, nix::sys::socket::sockopt::PeerCredentials)
        .map_err(io::Error::from)?;
    Ok(Some(PeerCreds {
        uid: creds.uid(),
        gid: creds.gid(),
        pid: creds.pid(),
    }))
}

#[cfg(not(target_os = "linux"))]
pub fn peer_credentials(_stream: &UnixStream) -> io::Result<Option<PeerCreds>> {
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stranger_uid() -> u32 {
        // Any uid that is neither 0 nor our own euid.
        let euid = nix::unistd::geteuid().as_raw();
        if euid == 42_424 {
            42_425
        } else {
            42_424
        }
    }

    #[test]
    fn root_is_always_permitted() {
        let policy = PeerPolicy::with_grants(&[], &[]);
        assert!(policy.permits(0, stranger_uid()));
    }

    #[test]
    fn own_euid_is_always_permitted() {
        let policy = PeerPolicy::with_grants(&[], &[]);
        assert!(policy.permits(nix::unistd::geteuid().as_raw(), stranger_uid()));
    }

    #[test]
    fn extra_uid_is_permitted() {
        let policy = PeerPolicy::with_grants(&[4242], &[]);
        assert!(policy.permits(4242, 999_999));
    }

    #[test]
    fn extra_gid_is_permitted() {
        let policy = PeerPolicy::with_grants(&[], &[5000]);
        assert!(policy.permits(stranger_uid(), 5000));
    }

    #[test]
    fn stranger_uid_and_gid_are_rejected() {
        let policy = PeerPolicy::with_grants(&[], &[]);
        assert!(!policy.permits(stranger_uid(), stranger_uid()));
    }

    #[test]
    fn with_grants_always_includes_root_and_own_euid() {
        let policy = PeerPolicy::with_grants(&[7], &[8]);
        assert!(policy.allowed_uids.contains(&0));
        assert!(policy
            .allowed_uids
            .contains(&nix::unistd::geteuid().as_raw()));
        assert!(policy.allowed_uids.contains(&7));
        assert_eq!(policy.allowed_gids, vec![8]);
    }

    /// `UnixStream::pair` peers are this process, so SO_PEERCRED must report
    /// our own euid and pid.
    #[cfg(target_os = "linux")]
    #[test]
    fn socketpair_reports_this_process_credentials() {
        let (a, _b) = UnixStream::pair().unwrap();
        let creds = peer_credentials(&a)
            .unwrap()
            .expect("Linux always provides SO_PEERCRED");
        assert_eq!(creds.uid, nix::unistd::geteuid().as_raw());
        assert_eq!(creds.pid, std::process::id() as i32);
    }

    #[cfg(not(target_os = "linux"))]
    #[test]
    fn non_linux_reports_no_credential_facility() {
        let (a, _b) = UnixStream::pair().unwrap();
        assert_eq!(peer_credentials(&a).unwrap(), None);
    }
}
