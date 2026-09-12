# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""SessionBundle: Client agent -> Perception agent. Frozen at P1.

Describes one capture session (Mode A or Mode B) handed from the phone
client to the perception pipeline. Carries a reference to the audio, not
the audio itself.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class SessionBundle(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: UUID
    user_id: UUID
    mode: Literal["mode_a", "mode_b"]
    started_at: datetime
    ended_at: datetime | None = None
    audio_ref: str
    sample_rate_hz: int
    codec: str
    language_hint: str | None = None
    device_id: str
    client_app_version: str
