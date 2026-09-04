"""Pydantic request models for the job-creation endpoints. Kept separate
from app.jobs.manager's DEFAULT_TUNING dict (the actual runtime source of
truth for defaults) -- these mirror it only for request validation/API
documentation; manager.build_probe_args() re-applies DEFAULT_TUNING itself
regardless of what a client actually sends.
"""
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field


class TuningConfig(BaseModel):
    program: Optional[int] = None
    pid_video: Optional[str] = Field(default=None, description="e.g. 0x101 or 257")
    pid_scte35: Optional[str] = Field(default=None, description="e.g. 0x1F0 or 496")
    codec: Optional[Literal["h264", "hevc"]] = None

    tolerance_ms: float = 6000.0
    max_early_ms: float = 50.0
    timeout_s: float = 12.0
    ok_threshold_ms: float = 41.0
    preroll_tolerance_ms: float = 500.0
    min_time_to_event_ms: float = 4000.0
    include_cra: bool = False

    snapshot_enabled: bool = True
    snapshot_all_idr: bool = False
    pre_frames: int = 0
    # "Which exact frame does time_to_event/pre-roll point to?" -- see
    # app.core.probe.Probe's __init__ docstring comment for the full
    # semantics of each mode.
    time_to_event_snapshot: Literal["off", "cue_arrival_frame", "target_pts_frame"] = "off"
    preroll_snapshot: Literal["off", "realtime_deadline_frame", "same_as_matched_idr"] = "off"

    segment_save_enabled: bool = True
    segment_window_s: float = 60.0
    segment_no_preroll: bool = False
    segment_max_files: Optional[int] = 500


class UdpSourceConfig(BaseModel):
    type: Literal["udp"] = "udp"
    addr: str = Field(..., description="Multicast group (e.g. 239.1.1.1) or unicast/loopback address")
    port: int
    iface: Optional[str] = None
    transport: Literal["auto", "ts", "rtp"] = "auto"


class HlsSourceConfig(BaseModel):
    type: Literal["hls"] = "hls"
    url: str


class DashSourceConfig(BaseModel):
    type: Literal["dash"] = "dash"
    url: str


class CreateLiveJobRequest(BaseModel):
    name: str
    source: Union[UdpSourceConfig, HlsSourceConfig, DashSourceConfig] = Field(discriminator="type")
    tuning: TuningConfig = Field(default_factory=TuningConfig)


class UpdateJobRequest(BaseModel):
    """PATCH /api/jobs/{job_id}. All fields optional -- only what's sent is
    changed. `source` is rejected for a "file" job (its uploaded file can't
    be swapped via edit -- create a new job for that) and its `type` must
    match the job's existing source_type (an edit can retune a udp job's
    address/port, not turn it into an hls job)."""
    name: Optional[str] = None
    source: Optional[Union[UdpSourceConfig, HlsSourceConfig, DashSourceConfig]] = Field(
        default=None, discriminator="type")
    tuning: Optional[TuningConfig] = None


class UpdateRetentionRequest(BaseModel):
    """PATCH /api/jobs/{job_id}/retention -- how long this job's saved
    segments (TS dumps)/snapshots are kept before the periodic sweep (or
    an on-demand cleanup) deletes them. None ("keep forever") is the
    default and matches pre-existing behavior exactly -- nothing is
    auto-deleted unless a limit is set. Unlike `tuning` above, this is
    NOT gated on the job being stopped: retention is enforced entirely
    from the parent process and never touches a running Probe."""
    segment_retention_s: Optional[float] = Field(default=None, ge=0)
    snapshot_retention_s: Optional[float] = Field(default=None, ge=0)


class CleanupRequest(BaseModel):
    """POST /api/jobs/{job_id}/cleanup -- an immediate, one-off purge,
    independent of the job's saved retention settings above (though you
    can pass the same values if you just want to apply them right now
    instead of waiting for the next periodic sweep). Omit a field to skip
    that category; 0 means "everything currently saved in that category"."""
    segment_max_age_s: Optional[float] = Field(default=None, ge=0)
    snapshot_max_age_s: Optional[float] = Field(default=None, ge=0)
