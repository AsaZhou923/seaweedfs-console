"""Small worker entrypoint placeholder.

The current implementation runs local fixture scans synchronously through the API
so tests and local trials are deterministic. Keeping this entrypoint makes the
deployment shape match the design and gives a stable place for an async worker
loop when real S3 queueing is enabled.
"""

from console.db import connect, init_db


def main() -> None:
    db = connect()
    init_db(db)
    print("worker ready: synchronous local jobs are handled by the API process")


if __name__ == "__main__":
    main()
