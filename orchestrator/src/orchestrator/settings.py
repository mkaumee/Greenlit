"""Runtime configuration, read once from the environment.

Everything cloud-shaped is built so it runs locally today and switches over
later by changing configuration rather than code. There is no GCP project yet,
so ``token_backend`` defaults to ``file`` and no Google Cloud client is ever
constructed. When the project exists, set ``CINEMA_TOKEN_BACKEND=secret-manager``
and nothing else changes.

Read from the environment with the ``CINEMA_`` prefix, or from a local ``.env``
that `.gitignore` already excludes.
"""

from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict

GMAIL_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
)
"""Send, and modify so we can clear the UNREAD label after reading.

Deliberately not ``gmail.readonly`` — polling has to mark mail read or every
tick re-reads the same replies. Deliberately not full ``mail.google.com``
either; the agent has no business deleting anything.
"""


class BrainBackend(StrEnum):
    SCRIPTED = "scripted"
    """Role B's deterministic fake. No LLM, no network, and not the product.

    The default only because ``main-agent`` lives on the other branch. It is a
    regex and a short noun list — good enough to prove the loop runs, nowhere
    near good enough to show anyone.
    """

    MAIN_AGENT = "main-agent"
    """Role A's real brain. Selected once role_a merges."""


class MailBackend(StrEnum):
    MEMORY = "memory"
    """No network. The default, so nothing accidentally emails a real supplier."""

    GMAIL = "gmail"
    """The real thing. Requires a refresh token to have been bootstrapped."""


class LogFormat(StrEnum):
    JSON = "json"
    """One object per line, with the keys Cloud Logging reads.

    The default. Not because it is nicer to read locally — it plainly is not —
    but because it is the format anyone will be reading at 3am when a
    negotiation has gone wrong on a deployed service, and a format only
    exercised in production is a format nobody has tested.
    """

    TEXT = "text"
    """Plain lines, for working at a terminal."""


class TokenBackend(StrEnum):
    FILE = "file"
    """A gitignored directory on disk. The default while there is no GCP project."""

    SECRET_MANAGER = "secret-manager"
    """Google Secret Manager. Flip to this when the project exists."""


class Settings(BaseSettings):
    """One object, built once at startup, passed down explicitly.

    Not a global and not read at point of use — a module that reaches for
    configuration on its own is a module that cannot be tested without setting
    environment variables.
    """

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_prefix="CINEMA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- identity ---------------------------------------------------------- #

    gcp_project: str = "demo-cinema"
    agent_email: str = "agent@example.invalid"
    agent_display_name: str = "Agentic Cinema"

    # -- databases ---------------------------------------------------------- #

    default_database: str = "(default)"
    """Projects, items, suppliers, negotiations, messages."""

    orders_database: str = "orders"
    """Purchase orders, and nothing else.

    A separate database because Firestore security rules do not apply to server
    SDKs, and Firestore IAM has no collection-level granularity. Those two facts
    together mean a single database cannot express "this service account may
    write negotiations but not orders" — ``roles/datastore.user`` is all or
    nothing across a database.

    So the boundary is the database. The tick service is granted access to
    ``default_database`` only, and never constructs a client for this one.
    """

    # -- transports -------------------------------------------------------- #

    brain_backend: BrainBackend = BrainBackend.SCRIPTED
    """Which reasoning implementation to run.

    Defaults to the fake because Role A's ``main-agent`` is not on this branch
    yet. That default is a liability rather than a convenience: a fake that
    ships looks exactly like a working system until someone reads a negotiation
    email. ``/health`` reports which one is live for that reason.
    """

    mail_backend: MailBackend = MailBackend.MEMORY
    """Defaults to memory on purpose.

    Real email to a real seller is not something to fall into because a token
    happened to be present. Sending for real is an explicit choice:
    ``CINEMA_MAIL_BACKEND=gmail``.
    """

    oauth_client_id: str = ""
    oauth_client_secret: str = ""

    # -- credentials ------------------------------------------------------- #

    token_backend: TokenBackend = TokenBackend.FILE
    token_dir: Path = Path(".secrets")
    """Where FILE-backed refresh tokens live. Gitignored; never committed."""

    oauth_client_secrets: Path = Path(".secrets/client_secret.json")
    """The downloaded OAuth client. Only ``scripts/oauth_bootstrap.py`` reads it."""

    refresh_token_secret: str = "gmail-agent-refresh-token"
    """Secret Manager secret name, used only when token_backend is secret-manager."""

    # -- approvals ---------------------------------------------------------- #

    auth_emulator_host: str = ""
    """Mirrors FIREBASE_AUTH_EMULATOR_HOST, for reporting on /health.

    firebase-admin reads the environment variable itself; this exists so the
    approval service can say out loud whether it is verifying real signatures
    or trusting the emulator's unsigned tokens. That is not a detail to have to
    infer from which terminal you are looking at.
    """

    # -- observability ------------------------------------------------------ #

    log_format: LogFormat = LogFormat.JSON
    log_level: str = "INFO"

    # -- loop -------------------------------------------------------------- #

    tick_limit: int = 50
    """How many due negotiations one tick may take. Bounded so a tick that gets
    killed has done a predictable amount of work."""

    poll_query: str = "is:unread -from:me"
    """Gmail search for the inbound poll. Excludes our own sent mail."""

    @property
    def refresh_token_path(self) -> Path:
        return self.token_dir / "gmail_refresh_token.json"
