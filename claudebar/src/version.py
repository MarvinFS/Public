"""Single source of truth for the application version.

build-installer.ps1 reads this file for its default -Version, so the number in
the log's start line and the number on the installer cannot drift apart.
"""

__version__ = "1.7.0"
