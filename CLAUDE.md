# CLAUDE.md — RustDesk API Server

## Project Overview

Build a modern, self-hosted **RustDesk API server and management WebUI**, implemented primarily in **Python**.

The project should provide a clean, maintainable alternative to existing community RustDesk API implementations while remaining compatible with the RustDesk ecosystem and clients wherever practical.

The initial reference/inspiration project is:

https://github.com/bryangerlach/rustdesk-api-server

Do **not** blindly copy its architecture or implementation. Use it primarily to understand the expected RustDesk API behavior, concepts, device lifecycle, authentication, and management functionality.

The project should be designed as a new implementation with:

* Python backend
* SQLite database by default
* Modern responsive WebUI
* REST API
* RustDesk-compatible API endpoints where required
* Authentication and authorization
* Device/client management
* User management
* Device groups/tags
* Device sharing
* Online/offline status
* Heartbeat handling
* API token/session management
* Audit logging
* Configuration through environment variables and/or configuration file
* Native "live" execution without Docker
* Docker support
* Linux, Windows, and macOS compatibility
* Development/testing primarily on **Windows**
* Production deployment on Linux and Docker
* No dependency on Linux-specific functionality in the application itself

The application is an **API/control plane and management layer**. It is not intended to reimplement the RustDesk `hbbs` ID/rendezvous server or `hbbr` relay server.

RustDesk's official architecture uses `hbbs` as the ID/rendezvous server and `hbbr` as the relay server. Keep that distinction clear in the application architecture.

---

# 1. Primary Goals

The project must prioritize:

1. RustDesk compatibility
2. Simplicity of deployment
3. Cross-platform operation
4. SQLite-first development
5. Excellent WebUI/UX
6. Clean API design
7. Security
8. Maintainability
9. Testability
10. Easy migration from development to production

The application should be usable with:

```text
Windows
Linux
macOS
Docker
```

The same Python application should run in all environments.

Avoid platform-specific behavior unless absolutely necessary.

---

# 2. Development Environment

## Primary Development Platform

Development and manual integration testing will initially happen on:

```text
Windows
```

Do not assume Linux is available during normal development.

All development commands must therefore work on Windows PowerShell.

Example:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m app
```

Do not require:

* bash
* systemd
* `/etc/...`
* `/opt/...`
* `/var/...`
* Unix shell utilities
* Linux-specific filesystem paths
* Linux-only Python packages

Linux support must still be maintained and tested through CI and/or Docker.

---

# 3. Runtime Requirements

The application must support:

## Native Python

Example:

```text
python -m app
```

or:

```text
python main.py
```

Prefer a proper Python package/module entry point:

```text
python -m rustdesk_api
```

The exact command should be documented and consistent.

## Docker

Provide:

```text
Dockerfile
docker-compose.yml
```

The Docker image should:

* use a minimal maintained Python base image
* run as a non-root user where practical
* persist SQLite data using a mounted volume
* expose the configured HTTP port
* use environment variables for configuration
* include a healthcheck
* gracefully handle SIGTERM/SIGINT
* not require an interactive shell

Example:

```yaml
services:
  rustdesk-api:
    build: .
    ports:
      - "21114:21114"
    volumes:
      - ./data:/app/data
    environment:
      RUSTDESK_API_HOST: 0.0.0.0
      RUSTDESK_API_PORT: 21114
      DATABASE_URL: sqlite:///./data/rustdesk.db
```

Do not hard-code these values into application logic.

---

# 4. Architecture

Use a layered architecture.

Recommended structure:

```text
project/
│
├── CLAUDE.md
├── README.md
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
│
├── src/
│   └── rustdesk_api/
│       ├── __init__.py
│       ├── __main__.py
│       ├── config.py
│       │
│       ├── api/
│       │   ├── __init__.py
│       │   ├── auth.py
│       │   ├── users.py
│       │   ├── devices.py
│       │   ├── groups.py
│       │   ├── tags.py
│       │   ├── shares.py
│       │   ├── heartbeat.py
│       │   ├── health.py
│       │   └── admin.py
│       │
│       ├── models/
│       │   ├── user.py
│       │   ├── device.py
│       │   ├── group.py
│       │   ├── tag.py
│       │   ├── share.py
│       │   ├── session.py
│       │   └── audit.py
│       │
│       ├── services/
│       │   ├── authentication.py
│       │   ├── devices.py
│       │   ├── heartbeat.py
│       │   ├── sharing.py
│       │   ├── tokens.py
│       │   └── audit.py
│       │
│       ├── db/
│       │   ├── database.py
│       │   └── migrations/
│       │
│       ├── web/
│       │   ├── templates/
│       │   └── static/
│       │
│       └── security/
│           ├── passwords.py
│           ├── tokens.py
│           └── permissions.py
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── compatibility/
│
└── data/
```

The exact framework may differ, but the separation of concerns should remain.

---

# 5. Backend Framework

Use a modern Python web framework.

Preferred:

```text
FastAPI
```

Use:

```text
Uvicorn
```

for development/runtime.

Reasons:

* excellent REST API support
* automatic OpenAPI documentation
* type hints
* async support
* good testing ecosystem
* straightforward Docker deployment
* suitable for both API and WebUI serving

Do not introduce Django unless there is a compelling architectural reason.

The application should remain relatively lightweight.

---

# 6. Database

SQLite is the default and first-class database.

Use:

```text
SQLAlchemy 2.x
```

or another mature ORM/database abstraction that supports SQLite cleanly.

The application must not scatter raw SQL throughout endpoint handlers.

Database access should go through models/repositories/services.

Example configuration:

```env
DATABASE_URL=sqlite:///./data/rustdesk.db
```

The application must automatically create the database directory when appropriate.

Never assume the current working directory is the project root.

Resolve relative paths predictably.

---

# 7. Database Design

At minimum support these entities:

### User

Fields should include concepts such as:

```text
id
username
email
password_hash
is_active
is_admin
created_at
updated_at
last_login_at
```

Do not store plaintext passwords.

---

### Device

A device should support information such as:

```text
id
rustdesk_id
name
hostname
alias
username
platform
os_version
client_version
ip_address
last_seen
last_online
password/credential metadata if required
created_at
updated_at
owner_id
```

Be careful with storing sensitive device information.

Credentials/secrets must never be logged.

---

### Device Group

Support logical grouping:

```text
id
name
description
owner_id
created_at
updated_at
```

Devices may belong to groups.

---

### Tags

Support:

```text
id
name
color
```

Use a many-to-many relationship between devices and tags.

---

### Device Shares

Support sharing a device with another user.

For example:

```text
device_id
owner_id
shared_with_user_id
permissions
created_at
expires_at
```

Permissions should be explicit rather than implicit.

---

### Sessions / Tokens

Maintain authenticated sessions or API tokens.

Tokens must be:

* cryptographically random
* revocable
* expirable
* stored securely
* never logged

Where possible, store a hash of a token rather than the raw token.

---

### Audit Log

Record important administrative actions.

Examples:

```text
login
logout
user_created
user_deleted
device_created
device_updated
device_deleted
device_shared
device_unshared
settings_changed
```

Never put passwords, API keys, session tokens, or other secrets into audit logs.

---

# 8. RustDesk Compatibility

Compatibility with actual RustDesk clients is more important than designing a theoretically perfect REST API.

Before implementing an endpoint, determine:

1. What RustDesk client calls it?
2. HTTP method
3. URL/path
4. Headers
5. Authentication mechanism
6. Request format
7. Response format
8. Required status codes
9. Error behavior
10. Version differences

Document compatibility-sensitive endpoints.

Do not arbitrarily rename or change RustDesk API endpoints merely to make them more RESTful.

If the RustDesk protocol expects a particular endpoint or payload, implement the expected protocol.

---

# 9. RustDesk Client Compatibility Testing

Create a dedicated compatibility test suite.

Example:

```text
tests/
└── compatibility/
    ├── test_login.py
    ├── test_devices.py
    ├── test_heartbeat.py
    ├── test_address_book.py
    └── test_authentication.py
```

Tests should verify actual HTTP behavior.

Do not only test internal Python functions.

Where practical, capture real RustDesk client requests and reproduce them in automated tests.

Maintain fixtures for:

```text
request headers
request JSON
request form data
response JSON
authentication tokens
```

Do not commit real credentials or sensitive device data.

---

# 10. API Versioning

Separate RustDesk compatibility endpoints from the project's own management API when useful.

For example:

```text
/api/rustdesk/...
/api/v1/...
```

Do not force RustDesk-specific protocol behavior into generic REST endpoints.

The API should have a clear OpenAPI specification.

Expose API documentation during development.

Production exposure of Swagger/OpenAPI should be configurable.

Example:

```env
API_DOCS_ENABLED=true
```

---

# 11. Authentication

Implement secure authentication from the beginning.

Support:

* login
* logout
* session/token expiration
* password hashing
* account activation/deactivation
* administrator role
* normal user role

Use a strong password hashing algorithm such as:

```text
Argon2id
```

Do not use:

```text
MD5
SHA1
plain SHA256
plaintext passwords
```

for password storage.

---

# 12. Authorization

Use explicit authorization checks.

At minimum:

```text
Administrator
User
```

A normal user must not be able to access another user's private devices merely by changing an ID in a URL.

Every device-related API operation must verify ownership or an explicit share permission.

Never rely on the WebUI to enforce security.

Authorization must happen on the server.

---

# 13. First-Run Administrator

Provide a safe first-run mechanism.

Possible behavior:

* application starts with no users
* setup page prompts for creation of the first administrator
* after the first administrator exists, setup is disabled

Do not automatically grant administrator privileges to arbitrary subsequent registrations.

If registration is enabled:

```env
ALLOW_REGISTRATION=false
```

should be the secure default.

---

# 14. WebUI

The WebUI should look like a modern SaaS/admin application.

Avoid the appearance of an old Django administration panel.

Recommended frontend technologies:

```text
React
TypeScript
Tailwind CSS
```

or another modern equivalent.

The UI should be:

* responsive
* desktop-friendly
* usable on tablets
* accessible
* fast
* visually consistent

Important screens:

### Login

* username/email
* password
* remember session where appropriate
* clear error messages

### Dashboard

Show:

```text
Total Devices
Online Devices
Offline Devices
Users
Groups
Recent Activity
```

Avoid unnecessary animations.

---

### Devices

Provide:

* searchable device table
* sorting
* filtering
* pagination
* online/offline indicator
* hostname
* RustDesk ID
* alias
* operating system
* last seen
* owner
* tags
* group

Device detail page should provide:

* device metadata
* connection information
* tags
* group
* sharing
* activity
* last heartbeat
* client version

---

### Users

Administrators should be able to:

* list users
* create users
* deactivate users
* delete users
* reset passwords
* view device ownership
* assign roles

---

### Groups

Support:

* create
* rename
* delete
* assign devices
* filter devices

---

### Tags

Support:

* create
* rename
* delete
* color selection
* device filtering

---

### Sharing

Provide an intuitive interface for:

```text
Share device → select user → select permissions → save
```

---

### Settings

Include application-level settings where appropriate.

Do not expose secrets in the UI after they have been saved.

---

# 15. WebUI Design Principles

Use a consistent design system.

Prefer:

```text
Cards
Tables
Badges
Dropdowns
Dialogs
Side navigation
Top navigation
Toast notifications
Empty states
Loading skeletons
```

Use clear states:

```text
Online
Offline
Unknown
Disabled
Pending
Error
```

Do not rely exclusively on color to communicate state.

---

# 16. Device Online Detection

Device online state should be derived from heartbeat/last-seen information.

Do not store an `online=true` value indefinitely without expiration logic.

Example configuration:

```env
DEVICE_ONLINE_TIMEOUT=120
```

If:

```text
current_time - last_seen > DEVICE_ONLINE_TIMEOUT
```

the device should be considered offline.

The timeout must be configurable.

Avoid background polling every device individually.

Calculate status efficiently from `last_seen`.

---

# 17. Heartbeats

Heartbeat handling is important.

The heartbeat endpoint should:

* authenticate the device/client where required
* update `last_seen`
* update relevant metadata
* update IP information where appropriate
* update client version
* update OS/platform information
* avoid unnecessary database writes

Do not create a new database record for every heartbeat.

Use an existing device record.

If a heartbeat contains new metadata, update only changed fields when practical.

---

# 18. Device Credentials

Treat RustDesk device passwords and similar credentials as highly sensitive.

Do not:

* print them in logs
* return them to unauthorized users
* store them unnecessarily
* put them in browser localStorage
* expose them in API debug output

If credentials must be persisted for compatibility, document exactly why.

Prefer encryption at rest over plaintext storage where the application actually needs to retrieve the value.

Encryption keys must come from configuration/environment, never from source code.

---

# 19. Configuration

Use environment variables with sensible defaults.

Example:

```env
RUSTDESK_API_HOST=0.0.0.0
RUSTDESK_API_PORT=21114

DATABASE_URL=sqlite:///./data/rustdesk.db

SECRET_KEY=change-me

ALLOW_REGISTRATION=false

DEVICE_ONLINE_TIMEOUT=120

LOG_LEVEL=INFO

API_DOCS_ENABLED=true

RUSTDESK_ID_SERVER=
RUSTDESK_RELAY_SERVER=
RUSTDESK_KEY=
```

Provide:

```text
.env.example
```

Never commit:

```text
.env
```

or real credentials.

---

# 20. Configuration Object

Centralize configuration.

Do not access:

```python
os.environ["SOME_VALUE"]
```

throughout the application.

Instead use a central configuration object, for example:

```python
from pydantic_settings import BaseSettings
```

Then inject/use the configuration throughout the application.

This makes testing substantially easier.

---

# 21. Logging

Use Python's standard logging infrastructure.

Provide structured and useful logs.

Levels:

```text
DEBUG
INFO
WARNING
ERROR
CRITICAL
```

Default:

```text
INFO
```

Never log:

* passwords
* API tokens
* session tokens
* encryption keys
* private keys
* device passwords
* authentication headers

Avoid logging full request bodies by default.

---

# 22. Error Handling

Provide consistent JSON errors.

Example:

```json
{
  "error": {
    "code": "DEVICE_NOT_FOUND",
    "message": "The requested device does not exist."
  }
}
```

Do not leak Python tracebacks to users in production.

Development mode may provide richer debugging.

---

# 23. Security

Security is a first-class requirement.

At minimum:

* password hashing
* CSRF protection where cookie-based authentication is used
* secure session handling
* authorization checks
* input validation
* rate limiting for authentication endpoints
* secure HTTP headers
* configurable CORS
* protection against SQL injection
* protection against XSS
* protection against CSRF
* safe file/path handling
* secret management

Do not assume that a private LAN makes security unnecessary.

---

# 24. CORS

CORS must be configurable.

Default should be restrictive.

Example:

```env
CORS_ORIGINS=
```

Do not default to:

```text
*
```

when credentials are involved.

---

# 25. CSRF

If authentication uses cookies, implement proper CSRF protection.

If bearer tokens are used for API access, ensure browser authentication does not accidentally create CSRF vulnerabilities.

Do not disable CSRF merely because development becomes inconvenient.

---

# 26. Rate Limiting

Authentication endpoints should be rate limited.

At minimum protect:

```text
login
registration
password reset
token generation
```

The implementation should work correctly in single-process SQLite development.

Do not introduce Redis as a mandatory dependency.

---

# 27. SQLite Considerations

SQLite is the default database.

Configure it appropriately for a web application.

Consider:

```text
WAL mode
foreign keys
busy timeout
reasonable connection handling
```

Do not make SQLite assumptions that prevent future support for PostgreSQL.

Database access should be abstract enough that PostgreSQL could be added later without rewriting the application.

However:

**Do not add PostgreSQL support until SQLite functionality is solid.**

SQLite is the primary development database.

---

# 28. Database Migrations

Use a real migration system.

Preferred:

```text
Alembic
```

Never require users to delete their database when the schema changes.

Migrations should be versioned.

Example:

```text
alembic upgrade head
```

The Docker startup process may automatically run migrations if this behavior is clearly documented and safe.

---

# 29. API Documentation

OpenAPI documentation should be generated automatically.

During development:

```text
/docs
/redoc
/openapi.json
```

Document:

* authentication
* request schemas
* response schemas
* errors
* permissions
* RustDesk-specific behavior

Add examples for important endpoints.

---

# 30. Testing

Use:

```text
pytest
```

Tests must run on Windows.

Minimum categories:

```text
unit tests
integration tests
API tests
database tests
authentication tests
authorization tests
RustDesk compatibility tests
```

Use temporary SQLite databases for tests.

Do not make tests depend on a developer's local database.

---

# 31. Test Command

The canonical command should be:

```powershell
pytest
```

A clean checkout should be able to run the test suite after installing dependencies.

Example:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

---

# 32. Formatting and Linting

Use modern Python tooling.

Recommended:

```text
Ruff
Pyright or mypy
pytest
```

Prefer a single configuration in:

```text
pyproject.toml
```

Run:

```text
ruff check .
ruff format --check .
pytest
```

Type checking should be introduced progressively rather than generating enormous amounts of type-ignore code.

---

# 33. Frontend Testing

If a separate TypeScript frontend is used, include:

```text
ESLint
Prettier
TypeScript
Vitest
```

where appropriate.

Frontend builds must succeed in CI.

The backend should serve the compiled frontend in production where practical.

---

# 34. Frontend/Backend Development

During development it is acceptable to run:

```text
Backend: http://localhost:21114
Frontend: http://localhost:5173
```

with a development proxy.

Production should preferably expose a single application URL:

```text
http://server:21114
```

or:

```text
https://rustdesk.example.com
```

The exact deployment architecture should not require users to understand frontend development tooling.

---

# 35. Static WebUI Deployment

A production build should produce static frontend assets.

The backend should be able to serve the compiled frontend.

Avoid requiring Node.js at runtime.

Node.js should only be needed for frontend development/building.

---

# 36. Docker Build

The Docker build should ideally be multi-stage:

```text
frontend build
        ↓
Python application image
```

The final image should not contain unnecessary build tooling.

Keep the final image small.

---

# 37. Data Persistence

All persistent application data must live in a configurable data directory.

Example:

```text
data/
├── rustdesk.db
└── ...
```

Do not store persistent data inside:

```text
/tmp
```

or the container image.

Docker users must be able to persist data using:

```yaml
volumes:
  - ./data:/app/data
```

---

# 38. Backups

Provide documentation for SQLite backups.

At minimum explain:

```text
stop application
copy database
restart application
```

For safer online backups, provide a SQLite-aware backup mechanism if practical.

Do not tell users that copying an actively-written SQLite database is always safe.

---

# 39. Health Endpoints

Provide:

```text
/health
/ready
```

Example:

```json
{
  "status": "ok"
}
```

Readiness should verify important dependencies such as database connectivity.

Docker healthcheck should use the health endpoint.

---

# 40. Graceful Shutdown

The application must correctly handle:

```text
SIGINT
SIGTERM
```

This is particularly important for Docker.

Database connections should close cleanly.

Background tasks should stop gracefully.

---

# 41. Background Tasks

Avoid introducing Celery/Redis unless there is a demonstrated requirement.

For initial functionality, use lightweight application background tasks where possible.

Potential background tasks:

```text
cleanup expired sessions
cleanup stale temporary records
periodic maintenance
```

Do not use background tasks for core database consistency.

Core operations must complete synchronously/transactionally.

---

# 42. Transactions

Use database transactions for multi-step operations.

For example:

```text
create user
create device share
remove device
change ownership
```

should not leave partially completed data when one step fails.

---

# 43. API Idempotency

Where appropriate, operations such as heartbeat/update should be idempotent.

Repeated requests should not create duplicate devices or records.

For example, if a client sends the same heartbeat twice:

```text
one device
```

must remain:

```text
one device
```

---

# 44. Device Identity

RustDesk device IDs must be treated as identifiers, not usernames.

Do not assume a RustDesk ID is globally unique forever without considering the protocol/version and deployment.

Use appropriate database constraints based on the actual compatibility requirements.

If uniqueness is scoped by user/server, document that explicitly.

---

# 45. RustDesk Server Configuration

The API server should support configuration for the associated RustDesk infrastructure.

Examples:

```text
ID server
Relay server
RustDesk key
API server URL
```

Do not attempt to automatically discover infrastructure using fragile assumptions.

Make these values explicit and configurable.

For example:

```env
RUSTDESK_ID_SERVER=example.com
RUSTDESK_RELAY_SERVER=example.com
RUSTDESK_KEY=
```

---

# 46. Important Architectural Boundary

Do not confuse these components:

```text
RustDesk Client
       |
       +----------------------+
       |                      |
       v                      v
   API Server              hbbs
       |                      |
       |                      v
       |                    hbbr
       |
       v
   WebUI / Management
```

The Python project manages API/control-plane functionality.

It does not need to become a replacement for:

```text
hbbs
hbbr
```

unless a future requirement explicitly changes the scope.

---

# 47. WebSocket Support

If RustDesk/WebUI functionality requires WebSockets, implement them explicitly.

Do not assume normal HTTP endpoints can substitute for WebSockets.

When supporting HTTPS deployment:

```text
Browser
   |
 HTTPS
   |
Reverse Proxy
   |
HTTP/WebSocket
   |
API Server
```

Ensure WebSocket upgrade headers work correctly through reverse proxies.

---

# 48. Reverse Proxy Compatibility

The application must work behind:

```text
Nginx
Caddy
Traefik
Apache
```

Do not require a reverse proxy for local development.

Support configurable:

```text
trusted hosts
proxy headers
CORS
external URL
```

Do not blindly trust `X-Forwarded-*` headers from arbitrary clients.

---

# 49. Windows Compatibility

Windows is the primary development environment.

Pay special attention to:

* path handling
* SQLite file locking
* subprocess behavior
* environment variables
* signal handling
* line endings
* PowerShell commands
* frontend build scripts

Use `pathlib`.

Never construct filesystem paths manually using `/`.

Bad:

```python
path = "data/" + filename
```

Good:

```python
from pathlib import Path

path = Path("data") / filename
```

---

# 50. Linux Compatibility

The application should run on modern Linux distributions without requiring unusual system packages.

Do not require:

```text
systemd
```

for the application itself.

A systemd service file may be provided as an optional deployment example.

---

# 51. macOS Compatibility

Do not use Linux-specific filesystem or process assumptions.

The native Python application should run on macOS using the same installation instructions as Windows/Linux with appropriate shell syntax.

---

# 52. CLI

Provide a small CLI.

Examples:

```text
rustdesk-api --help
rustdesk-api serve
rustdesk-api migrate
rustdesk-api create-admin
rustdesk-api version
```

Do not make management tasks require manually editing the SQLite database.

Useful commands:

```text
create-admin
reset-password
list-users
migrate
check-config
```

---

# 53. Initial Admin CLI

Provide a way to create an administrator without relying exclusively on the WebUI.

Example:

```text
python -m rustdesk_api create-admin
```

Prompt securely for:

```text
username
password
```

Do not echo the password.

---

# 54. Documentation

README.md must contain:

1. Project description
2. Features
3. Requirements
4. Windows installation
5. Linux installation
6. macOS installation
7. Docker installation
8. Configuration
9. Database
10. First-run setup
11. RustDesk client configuration
12. Reverse proxy setup
13. Security recommendations
14. Backup/restore
15. API documentation
16. Development instructions
17. Testing
18. Troubleshooting

---

# 55. Windows Quick Start

The documentation should make this easy:

```powershell
git clone <repository>
cd <repository>

python -m venv .venv
.venv\Scripts\Activate.ps1

pip install -e ".[dev]"

python -m rustdesk_api
```

Then open:

```text
http://127.0.0.1:21114
```

Do not make developers manually configure SQLite.

The application should create its initial database automatically or provide a simple initialization command.

---

# 56. Docker Quick Start

The documentation should make this possible:

```bash
docker compose up -d
```

Then:

```text
http://127.0.0.1:21114
```

Persistent data must survive:

```bash
docker compose down
docker compose up -d
```

---

# 57. Versioning

Use semantic versioning:

```text
MAJOR.MINOR.PATCH
```

Example:

```text
0.1.0
```

during initial development.

Do not claim stable compatibility until actual RustDesk client compatibility testing has been performed.

---

# 58. Compatibility Matrix

Maintain a compatibility document:

```text
docs/rustdesk-compatibility.md
```

Track:

| RustDesk Client | API Compatibility | Tested |
| --------------- | ----------------- | ------ |
| Version X       | Full/Partial      | Yes/No |

Do not claim compatibility based only on theoretical API similarity.

---

# 59. Dependency Policy

Keep dependencies to a reasonable minimum.

Before adding a dependency ask:

1. Is it necessary?
2. Is it maintained?
3. Does it work on Windows?
4. Does it work on Linux?
5. Does it work on macOS?
6. Does it work with SQLite?
7. Does it complicate Docker?
8. Does it introduce unnecessary security risk?

Avoid dependencies that only work on Linux.

---

# 60. Secrets

Never commit:

```text
.env
*.pem
*.key
passwords
API tokens
JWT secrets
RustDesk private keys
production databases
```

Provide:

```text
.env.example
```

with placeholders.

---

# 61. Git Hygiene

Do not commit:

```text
.venv/
__pycache__/
.pytest_cache/
node_modules/
dist/
build/
data/*.db
.env
```

Use an appropriate `.gitignore`.

---

# 62. Code Quality

Prefer:

* small functions
* explicit types
* dependency injection
* clear names
* immutable configuration
* service classes/functions for business logic
* thin API handlers
* reusable validation
* meaningful exceptions

Avoid:

* huge endpoint functions
* global mutable state
* duplicated authentication logic
* duplicated database queries
* hidden side effects
* hard-coded paths
* hard-coded secrets

---

# 63. API Handler Rule

API route handlers should be thin.

Bad:

```text
route
 ├── validation
 ├── authentication
 ├── SQL queries
 ├── business logic
 ├── password processing
 ├── logging
 └── response generation
```

Prefer:

```text
route
   ↓
authentication
   ↓
validation
   ↓
service
   ↓
repository/database
   ↓
response
```

---

# 64. Error Handling Rule

Never use:

```python
except Exception:
    pass
```

Do not silently swallow errors.

If an error is intentionally ignored, document why.

---

# 65. Security Review Rule

Before implementing authentication, tokens, device credentials, sharing, or administrative functionality, explicitly consider:

```text
authentication
authorization
data exposure
CSRF
XSS
SQL injection
session theft
token theft
privilege escalation
IDOR
```

Especially test for IDOR:

```text
GET /api/devices/123
```

must not allow user A to access device 123 owned by user B merely because the ID is known.

---

# 66. UI Security

Never trust frontend permissions.

The WebUI may hide administrator-only buttons, but the backend must enforce the same permissions.

For example:

```text
User hides "Delete User"
```

is not security.

The API must reject:

```text
DELETE /api/users/123
```

when the caller lacks permission.

---

# 67. Observability

Provide enough information to troubleshoot deployments.

Health endpoint:

```text
/health
```

Version endpoint:

```text
/api/version
```

Logs should identify:

```text
timestamp
level
component
request ID
event
```

A request ID should be generated for API requests where practical.

Do not include sensitive request content.

---

# 68. Performance

Do not prematurely optimize.

However:

* paginate device/user lists
* index frequently searched fields
* avoid N+1 queries
* avoid loading thousands of devices into memory
* use database-level filtering
* use efficient heartbeat updates
* avoid polling the database excessively

Important SQLite indexes should include fields such as:

```text
rustdesk_id
owner_id
last_seen
username
email
group_id
```

where justified by actual query patterns.

---

# 69. Pagination

All potentially large collections must support pagination.

Example:

```text
GET /api/devices?page=1&page_size=50
```

Return metadata such as:

```json
{
  "items": [],
  "page": 1,
  "page_size": 50,
  "total": 0
}
```

Do not return an unlimited device list.

---

# 70. Search

Device search should support useful fields such as:

```text
RustDesk ID
hostname
alias
username
IP
platform
```

Search must be performed server-side.

Do not download the entire database into the browser and filter locally.

---

# 71. Auditability

Administrative changes should be traceable.

For important actions record:

```text
actor
action
target
timestamp
IP where appropriate
result
```

Do not record secrets.

---

# 72. Backwards Compatibility

Do not casually change API response formats.

If a RustDesk client depends on an existing response:

```text
preserve it
```

If the project's own API needs a breaking change, version it.

---

# 73. Reference Implementation

Use the existing project:

https://github.com/bryangerlach/rustdesk-api-server

as a behavioral reference.

Study it for:

* RustDesk API endpoints
* authentication
* heartbeat behavior
* device fields
* device status
* WebUI concepts
* sharing
* tags
* aliases
* configuration
* deployment patterns

Do not copy bugs or architectural limitations merely because the reference project does something a particular way.

The reference implementation currently demonstrates Python/Django deployment, SQLite support, Docker deployment, device information, aliases, tags, online statistics, device sharing, token handling, and administration functionality.

---

# 74. Official RustDesk Behavior

When there is disagreement between a community implementation and current official RustDesk documentation/protocol behavior:

1. Verify the actual client behavior.
2. Check official RustDesk documentation/source.
3. Prefer observed protocol compatibility over assumptions.
4. Document version-specific behavior.

RustDesk's current self-hosting documentation identifies `hbbs` as the ID/rendezvous service and `hbbr` as the relay service, with API/WebSocket-related ports alongside the core signaling/relay ports.

---

# 75. Do Not Overbuild

The first version should NOT attempt to implement everything.

Prioritize:

### Phase 1

```text
Application bootstrap
SQLite
configuration
users
authentication
RustDesk-compatible API foundation
device registration
heartbeat
device listing
device detail
basic WebUI
Docker
Windows development
tests
```

### Phase 2

```text
groups
tags
sharing
admin UI
audit logs
better search/filtering
API documentation
```

### Phase 3

```text
advanced WebUI
WebSocket functionality
additional authentication providers
OIDC
LDAP
advanced reporting
```

Do not introduce Phase 3 complexity into Phase 1.

---

# 76. Definition of Done

A feature is not complete merely because the Python code works.

A feature is complete when:

* backend implementation exists
* database migration exists if required
* API endpoint is tested
* authorization is tested
* WebUI is implemented where applicable
* error states are handled
* documentation is updated
* Windows development works
* Docker still builds
* tests pass
* no secrets are exposed
* code is formatted/linted

---

# 77. Development Workflow

For each feature:

```text
1. Understand requirement
2. Check RustDesk compatibility requirements
3. Design data model
4. Create migration
5. Implement service/business logic
6. Implement API
7. Add tests
8. Implement WebUI
9. Test manually on Windows
10. Test Docker
11. Update documentation
12. Run lint/type checks
13. Run full test suite
```

Do not start by building the WebUI before the API/data model is understood.

---

# 78. Claude Coding Rules

When working on this repository:

### Before changing code

Inspect:

```text
CLAUDE.md
README.md
pyproject.toml
existing architecture
tests
database migrations
```

Understand existing patterns before introducing new ones.

### Before adding a dependency

Explain why it is needed internally and verify cross-platform support.

### Before changing an API endpoint

Search for:

```text
frontend usage
tests
RustDesk compatibility tests
documentation
```

Do not break existing callers unintentionally.

### Before changing database models

Create a migration.

Do not manually modify the production schema.

---

# 79. Do Not Rewrite Working Code Unnecessarily

Prefer incremental changes.

Do not replace:

* the framework
* database layer
* authentication system
* frontend stack

unless there is a concrete reason.

Avoid large speculative refactors.

---

# 80. Testing Philosophy

Tests should prove behavior.

Prefer:

```text
Given
When
Then
```

Examples:

```text
Given an authenticated user
When they request their devices
Then only devices they are authorized to see are returned.
```

And:

```text
Given a device heartbeat
When the heartbeat is received
Then last_seen is updated
and no duplicate device is created.
```

---

# 81. Final Quality Gate

Before considering a release ready, run:

```powershell
ruff check .
ruff format --check .
pytest
```

Then verify:

```text
python -m rustdesk_api
```

works from a clean Windows environment.

Then verify:

```text
docker compose build
docker compose up -d
```

works from a clean environment.

Confirm:

```text
WebUI loads
login works
database persists
device API works
heartbeat works
authorization works
health endpoint works
container restarts without losing data
```

---

# 82. Most Important Principle

**Build for actual RustDesk compatibility first, then build the management experience around it.**

The project should feel like a polished modern application, but the WebUI must never dictate or corrupt the underlying RustDesk API behavior.

When uncertain about protocol behavior:

```text
observe → test → document → implement
```

rather than:

```text
assume → implement → hope
```

The final application should be straightforward to run for a user who knows nothing about Python:

```text
Docker:
docker compose up -d
```

and straightforward for a developer:

```text
python -m rustdesk_api
```

while remaining portable across:

```text
Windows
Linux
macOS
Docker
```
