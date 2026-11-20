# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Notion settings (env prefix `NOTION_`, docs/pivot/SETTINGS_PATTERN.md).

`NOTION_TOKEN` is the internal integration secret (H5). It is a hidden
`SecretStr`: validation errors never echo it (`hide_input_in_errors`), and the
runtime must add it to the log scrubber when the tool is wired (INV-7). Unset
means the Notion tools stay unbound. The integration can read and write only
the pages the user explicitly shared with it in Notion.
"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class NotionSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NOTION_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    token: SecretStr | None = None
