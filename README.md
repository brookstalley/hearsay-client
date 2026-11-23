# hearsay-client

A Python client for a Hearsay service's wire contract. It's async, needs Python 3.12 or later, and its one dependency is `httpx`.

Opening the client runs the handshake. The client refuses a service in another environment, or one that doesn't serve this client's contract version, before any data call.

```python
from hearsay_client import HearsayClient

async with HearsayClient("http://hearsay.internal:8077", key, environment="dev") as hearsay:
    seen = await hearsay.lookup_url("https://example.org/a")
    if seen.witnesses:
        page = await hearsay.document(seen.witnesses[0].document)
```

A failed call raises `HearsayError` with the contract's error code. It never returns an empty answer in place of one. Idempotent calls are retried when the service answers with a retryable error, and each tell keeps its idempotency key across retries. A service that doesn't answer fails the call at once, as `unreachable`.

## Installing

Pin by commit:

```toml
dependencies = ["hearsay-client @ git+https://github.com/brookstalley/hearsay-client@<sha>"]
```

The service's source is private. This repository is published from it, and each commit here names the service commit its files came from.
