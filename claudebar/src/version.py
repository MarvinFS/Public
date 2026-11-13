"""Single source of truth for the application version.

build-installer.ps1 reads this file for its default -Version, so the number in
the log's start line and the number on the installer cannot drift apart.
"""

__version__ = "1.9.0"

# What ClaudeBar calls itself on its unauthenticated requests (exchange rates, the price
# catalog, provider marks). The Anthropic and ChatGPT calls keep their own agent strings
# in oauth_usage and openai_usage.
USER_AGENT = f"ClaudeBar/{__version__}"
