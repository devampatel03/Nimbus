# Copyright 2026 Devam Patel
# Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

"""Invisible / dangerous characters, built with chr() so no tool or formatter
can silently rewrite them into literals in test sources."""

ZWSP = chr(0x200B)
ZWJ = chr(0x200D)
BOM = chr(0xFEFF)
RLO = chr(0x202E)
PDF = chr(0x202C)
COMBINING_ACUTE = chr(0x0301)
LONE_SURROGATE = chr(0xD800)
ESC = chr(0x1B)
DEL = chr(0x7F)
C1 = chr(0x9F)
