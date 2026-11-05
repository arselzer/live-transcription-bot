"""Optional Hermes hook using the same bounded watchdog as the CLI.

Invoke with --dry-run for inspection. No Hermes SDK is needed by the application.
"""


def main(argv=None):
    from live_transcript.runtime.watchdog import main as watchdog

    return watchdog(argv)


if __name__ == "__main__":
    main()
