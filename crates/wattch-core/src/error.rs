use thiserror::Error;

pub type Result<T> = std::result::Result<T, Error>;

#[derive(Debug, Error)]
pub enum Error {
    #[error("missing required field: {0}")]
    MissingField(&'static str),

    #[error("duplicate local source id: {0}")]
    DuplicateLocalSourceId(u32),

    #[error("duplicate source uid: {0}")]
    DuplicateSourceUid(String),

    #[error("invalid source descriptor `{source_uid}`: {reason}")]
    InvalidSourceDescriptor { source_uid: String, reason: String },

    #[error("invalid sample: {0}")]
    InvalidSample(String),
}
