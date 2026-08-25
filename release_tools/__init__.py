"""Dependency-free SomethingBound release and launcher foundation tools."""

# The launcher's own version, and the single source of truth for it. The frozen
# executable carries whatever this said when it was built, and publishing the
# launcher channel reads the same constant, so the running launcher and the
# channel it checks can never disagree about what version means.
__version__ = "0.1.2"

from .channel_manifest import (
    ChannelManifestValidationError,
    load_channel_manifest,
    validate_channel_manifest,
    verify_channel_artifact,
)
from .manifest import (
    ManifestValidationError,
    compare_versions,
    load_manifest,
    validate_manifest,
    verify_artifact,
)
from .patch_notes import render_patch_notes
from .update_engine import (
    ArtifactVerificationError,
    AtomicInstallError,
    InstalledBuild,
    InstalledStateError,
    UpdateEngine,
    UpdateError,
    UpdateResult,
    atomic_install,
    download_and_verify,
    update_available,
)

__all__ = [
    "ArtifactVerificationError",
    "AtomicInstallError",
    "ChannelManifestValidationError",
    "InstalledBuild",
    "InstalledStateError",
    "ManifestValidationError",
    "UpdateEngine",
    "UpdateError",
    "UpdateResult",
    "atomic_install",
    "compare_versions",
    "download_and_verify",
    "load_channel_manifest",
    "load_manifest",
    "render_patch_notes",
    "update_available",
    "validate_channel_manifest",
    "validate_manifest",
    "verify_artifact",
    "verify_channel_artifact",
]
