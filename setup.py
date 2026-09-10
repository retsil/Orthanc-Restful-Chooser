#!/usr/bin/env python3

# Copyright (C) 2026 retsil <https://github.com/retsil/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Compatibility shim for tools that still invoke setup.py directly.

Every piece of packaging metadata -- name, version, requirements, packages,
entry points -- lives in pyproject.toml, and setuptools reads it from there.
This file therefore passes no arguments of its own; anything added here would
be a second declaration able to disagree with the first.
"""

from setuptools import setup

setup()
